"""
FastAPI 后端：提供用户偏好、历史简报、手动生成简报的 REST 接口，
并托管前端静态页面。
"""

import os
import secrets
import uuid
import threading
from datetime import datetime
from typing import Any, Dict, List

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
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

# ---- 安全配置：两项都是可选的，默认值保持「本机零配置即可跑」 ----
# API_KEY 留空 = 不启用鉴权（默认）；填了值则所有 /api 接口都要求请求头 X-API-Key 匹配。
# 只在需要对外暴露（内网/公网）时才设置，避免任何人都能触发一次完整 Agent 运行。
API_KEY = os.getenv("API_KEY", "").strip()

# CORS 允许来源：默认只放本机地址，不再使用通配符 "*"。
# 需要额外来源时，在 .env 的 CORS_ORIGINS 里用逗号分隔补充。
CORS_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()
] or ["http://127.0.0.1:8000", "http://localhost:8000"]

app = FastAPI(title="每日新闻助手 Agent", lifespan=lifespan)

# 启动时确保数据表存在（表已存在则无副作用）
try:
    db.init_db()
except Exception as e:  # 数据库不可用时仍允许服务启动，接口会返回明确错误
    print(f"[警告] 数据库初始化失败，请检查连接后执行 python migrate.py：{e}")

# 跨域：仅允许白名单来源（默认本机），不再是 allow_origins=["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: str = Header(default="")):
    """
    可选的接口鉴权。

    .env 里的 API_KEY 为空时直接放行（本机演示零配置即可使用）；
    设置了值之后，请求必须带 X-API-Key 头，否则返回 401。
    用 compare_digest 做定长比较，避免通过响应时间反推密钥。
    """
    if not API_KEY:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(
            status_code=401,
            detail="缺少或错误的访问密钥，请在请求头 X-API-Key 中提供正确的 API_KEY",
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


@app.get("/api/health")
def health():
    """
    健康检查（不需要鉴权）。
    前端据此判断服务端是否开启了鉴权，从而决定要不要提示输入访问密钥。
    """
    return {"status": "ok", "auth_required": bool(API_KEY)}


@app.get("/api/preferences")
def get_preferences(_auth=Depends(require_api_key)):
    """
    获取当前用户偏好。
    读取失败时返回 500，而不是把数据库故障伪装成「偏好就是默认值」。
    """
    prefs = load_preferences()
    if "error" in prefs:
        raise HTTPException(status_code=500, detail=prefs["error"])
    return prefs


@app.post("/api/preferences")
def update_preferences(pref: Preferences, _auth=Depends(require_api_key)):
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

# 单飞锁：一次只允许跑一个生成任务。
# 否则连点几次按钮 / 多标签页并发就会同时跑多个 Agent，
# 既浪费 token，也可能把同一份简报写好几次。
_generation_lock = threading.Lock()


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

    # 无论成功失败都要释放单飞锁，否则后续请求会被永久拒绝
    if _generation_lock.locked():
        try:
            _generation_lock.release()
        except RuntimeError:
            pass


@app.post("/api/generate")
def generate_digest_endpoint(background_tasks: BackgroundTasks, _auth=Depends(require_api_key)):
    """
    提交“生成今日简报”任务后立即返回 task_id。
    Agent 在后台自主执行（可能耗时 1-3 分钟），前端轮询 /api/generate/{task_id} 取结果。
    已有任务在跑时返回 409，避免并发重复生成。
    """
    if not _generation_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="已有生成任务正在执行，请等待其完成后再试",
        )

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
def get_generate_task(task_id: str, _auth=Depends(require_api_key)):
    """查询生成任务的状态与结果。"""
    with _tasks_lock:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="未找到该任务")
    return {"task_id": task_id, **task}


@app.get("/api/history")
def get_history(_auth=Depends(require_api_key)):
    """
    获取历史简报列表，按时间倒序（数据库查询已排序）。

    读取失败返回 500：绝不能用空列表冒充「还没有生成过简报」，
    否则数据库故障在页面上看起来和「真的没有数据」一模一样。
    """
    try:
        history = load_history()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取历史简报失败（请检查数据库是否可用）: {e}",
        )
    return {"history": history}


@app.get("/api/history/{digest_id}")
def get_history_item(digest_id: str, _auth=Depends(require_api_key)):
    """获取单条历史简报。"""
    item = db.get_history_item(digest_id)
    if not item:
        raise HTTPException(status_code=404, detail="未找到该简报")
    return item


@app.get("/api/trace/{trace_id}")
def get_trace(trace_id: str, _auth=Depends(require_api_key)):
    """
    获取某次 Agent 运行的工具调用轨迹。
    这是「工具调用顺序由 LLM 决定」的直接证据：可以看到模型实际调了哪些工具、什么顺序。
    """
    return {"trace_id": trace_id, "steps": db.get_trace(trace_id)}


# 预览单飞锁：一次预览要跑完整检索（Tavily 单次 13-21 秒）再加一次模型打分，
# 前端连点按钮或在多个标签页并发会成倍消耗额度并互相拖慢，因此并发请求直接 409。
_preview_lock = threading.Lock()


@app.post("/api/preview")
def preview_digest(req: DigestPreviewRequest, _auth=Depends(require_api_key)):
    """
    预览：根据当前偏好和可选查询词生成简报，但不发送邮件、不保存历史。
    用于前端快速查看效果。同一时刻只允许一个预览在执行。
    """
    if not _preview_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="已有预览正在执行，请等待其完成（通常需要 30-60 秒）",
        )
    try:
        preferences = load_preferences()
        if "error" in preferences:
            raise HTTPException(status_code=500, detail=preferences["error"])
        query = req.query.strip() or " ".join(
            preferences.get("topics", []) + preferences.get("keywords", [])
        )
        news = search_news(query, max_results=preferences.get("max_articles", 5))
        digest = generate_digest(news, preferences)
        return {
            "query": query,
            "news": news,
            "digest": digest,
            "created_at": datetime.now().isoformat(),
        }
    finally:
        # 必须放在 finally：请求中途抛错也要释放，否则预览会被永久锁死
        _preview_lock.release()


# 托管前端静态页面
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        """
        未匹配到路由时返回前端 index.html（单页应用的前端路由回退）。

        但 /api 前缀必须排除：否则 GET /api/definitely-not-a-route 会返回
        200 + HTML，前端拿它当 JSON 解析必然报错，问题会被掩盖。
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"接口不存在: /{full_path}")

        index_path = os.path.join(frontend_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="前端页面不存在")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
