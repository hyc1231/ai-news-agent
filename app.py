"""
FastAPI 后端：提供用户偏好、历史简报、手动生成简报的 REST 接口，
并托管前端静态页面。
"""

import os
import uuid
import threading
from datetime import datetime
from typing import Any, Dict, List

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from agent import generate_and_send_digest
from tools.file_tools import load_preferences, save_preferences, load_history, append_to_history
from tools.search_tools import search_news
from tools.digest_tools import generate_digest
from scheduler import lifespan
import db

load_dotenv()

app = FastAPI(title="每日新闻助手 Agent", lifespan=lifespan)

# 启动时确保数据表存在（表已存在则无副作用）
try:
    db.init_db()
except Exception as e:  # 数据库不可用时仍允许服务启动，接口会返回明确错误
    print(f"[警告] 数据库初始化失败，请检查连接后执行 python migrate.py：{e}")

# 允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 数据模型
class Preferences(BaseModel):
    name: str = ""
    email: str = ""
    topics: List[str] = Field(default_factory=lambda: ["人工智能", "大模型", "AI 产品"])
    keywords: List[str] = Field(default_factory=lambda: ["OpenAI", "Google", "DeepSeek"])
    language: str = "zh"
    max_articles: int = 5


class DigestPreviewRequest(BaseModel):
    query: str = ""


# API 路由
@app.get("/")
def root():
    """访问根路径时直接返回前端页面，没有前端则返回状态信息。"""
    index_path = os.path.join(os.path.dirname(__file__), "frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "每日 AI 新闻助手 Agent 已启动"}


@app.get("/api/preferences")
def get_preferences():
    """获取当前用户偏好。"""
    return load_preferences()


@app.post("/api/preferences")
def update_preferences(pref: Preferences):
    """保存用户偏好。"""
    data = pref.model_dump()
    result = save_preferences(data)
    if result.startswith("保存偏好失败"):
        raise HTTPException(status_code=500, detail=result)
    return {"status": "ok", "message": result, "data": data}


# ---- 生成任务的异步执行：避免前端被长时间阻塞 ----
# 仅保存在进程内存中，重启即失效，够用且无需引入额外依赖
TASKS: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()
MAX_TASKS = 50


def _run_generate_task(task_id: str):
    """后台线程：调用 Agent 生成简报，成功则写入历史（并关联工具调用轨迹）。"""
    try:
        result = generate_and_send_digest(trace_id=task_id)
    except Exception as e:
        result = {
            "success": False,
            "answer": f"生成简报时发生异常: {e}",
            "trace": [],
        }

    if result.get("success"):
        append_to_history(
            result.get("answer", ""),
            auto=False,
            trace_id=result.get("trace_id", ""),
        )

    with _tasks_lock:
        TASKS[task_id] = {
            "status": "success" if result.get("success") else "failed",
            "result": result,
            "finished_at": datetime.now().isoformat(),
        }


@app.post("/api/generate")
def generate_digest_endpoint(background_tasks: BackgroundTasks):
    """
    提交“生成今日简报”任务后立即返回 task_id。
    Agent 在后台自主执行（可能耗时 1-3 分钟），前端轮询 /api/generate/{task_id} 取结果。
    """
    task_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]

    with _tasks_lock:
        # 简单清理：任务过多时丢弃最早的已完成记录
        if len(TASKS) > MAX_TASKS:
            finished = [k for k, v in TASKS.items() if v.get("status") != "running"]
            for key in finished[: max(0, len(TASKS) - MAX_TASKS)]:
                TASKS.pop(key, None)
        TASKS[task_id] = {
            "status": "running",
            "result": None,
            "created_at": datetime.now().isoformat(),
        }

    background_tasks.add_task(_run_generate_task, task_id)
    return {"task_id": task_id, "status": "running", "message": "任务已提交，请轮询 /api/generate/{task_id} 获取结果"}


@app.get("/api/generate/{task_id}")
def get_generate_task(task_id: str):
    """查询生成任务的状态与结果。"""
    with _tasks_lock:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="未找到该任务")
    return {"task_id": task_id, **task}


@app.get("/api/history")
def get_history():
    """获取历史简报列表，按时间倒序（数据库查询已排序）。"""
    return {"history": load_history()}


@app.get("/api/history/{digest_id}")
def get_history_item(digest_id: str):
    """获取单条历史简报。"""
    item = db.get_history_item(digest_id)
    if not item:
        raise HTTPException(status_code=404, detail="未找到该简报")
    return item


@app.get("/api/trace/{trace_id}")
def get_trace(trace_id: str):
    """
    获取某次 Agent 运行的工具调用轨迹。
    这是「工具调用顺序由 LLM 决定」的直接证据：可以看到模型实际调了哪些工具、什么顺序。
    """
    return {"trace_id": trace_id, "steps": db.get_trace(trace_id)}


@app.post("/api/preview")
def preview_digest(req: DigestPreviewRequest):
    """
    预览：根据当前偏好和可选查询词生成简报，但不发送邮件、不保存历史。
    用于前端快速查看效果。
    """
    preferences = load_preferences()
    query = req.query.strip() or " ".join(preferences.get("topics", []) + preferences.get("keywords", []))
    news = search_news(query, max_results=preferences.get("max_articles", 5))
    digest = generate_digest(news, preferences)
    return {
        "query": query,
        "news": news,
        "digest": digest,
        "created_at": datetime.now().isoformat(),
    }


# 托管前端静态页面
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        """未匹配到 API 路由时，返回前端 index.html。"""
        index_path = os.path.join(frontend_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="前端页面不存在")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
