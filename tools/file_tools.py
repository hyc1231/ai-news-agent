"""
文件相关工具：列出目录、读取文件、写入文件、搜索内容、加载/保存用户偏好。
所有路径都基于项目根目录，避免工具随意访问系统任意位置。
"""

import os
import json
from pathlib import Path
from typing import Union, List, Dict, Any

import db
from tools.security import (
    PROJECT_ROOT,
    SEARCH_SKIP_DIRS,
    is_sensitive as _is_sensitive,
    safe_path as _safe_path,
)

# 项目根目录：ai-news-agent 文件夹（PROJECT_ROOT 由 tools.security 统一定义）
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _read_guard(path: str) -> Path:
    """读取前的统一检查：路径合法且不是敏感文件。"""
    target = _safe_path(path)
    if _is_sensitive(target):
        raise PermissionError(f"该文件包含敏感信息，不允许读取: {path}")
    return target


def list_dir(path: str = "") -> str:
    """
    列出目录内容。
    参数 path 为相对项目根目录的路径，空字符串表示项目根目录。
    返回文件和子目录列表，每项标注 [FILE] 或 [DIR]。
    """
    try:
        target = _safe_path(path) if path else PROJECT_ROOT
    except ValueError as e:
        return str(e)
    if not target.exists():
        return f"目录不存在: {path}"
    if not target.is_dir():
        return f"不是目录: {path}"

    items = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    lines = []
    for item in items:
        label = "[DIR] " if item.is_dir() else "[FILE]"
        lines.append(f"{label} {item.name}")
    return "\n".join(lines) if lines else "（空目录）"


def read_file(path: str) -> str:
    """
    读取文件内容。
    参数 path 为相对项目根目录的路径。
    敏感文件（如 .env）会被拒绝读取。
    """
    try:
        target = _read_guard(path)
    except (ValueError, PermissionError) as e:
        return str(e)
    if not target.exists():
        return f"文件不存在: {path}"
    if not target.is_file():
        return f"不是文件: {path}"
    try:
        return target.read_text(encoding="utf-8")
    except Exception as e:
        return f"读取文件失败: {e}"


def write_file(path: str, content: str) -> str:
    """
    写入文件内容。如果目录不存在会自动创建。
    参数 path 为相对项目根目录的路径。
    敏感文件（如 .env）不允许写入，防止篡改配置或注入内容。
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)
    if _is_sensitive(target):
        return f"该文件属于敏感文件，不允许写入: {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"写入成功: {path}"
    except Exception as e:
        return f"写入文件失败: {e}"


def search_content(keyword: str, dir: str = "") -> str:
    """
    在指定目录下按关键词搜索文件内容。
    只搜索文本文件（.txt, .md, .json, .py, .html, .js, .css），
    会跳过 .env 等含密钥的敏感文件，返回匹配的文件路径和行号列表。
    """
    try:
        target_dir = _safe_path(dir) if dir else PROJECT_ROOT
    except ValueError as e:
        return str(e)
    if not target_dir.exists() or not target_dir.is_dir():
        return f"目录不存在: {dir}"

    text_exts = {".txt", ".md", ".json", ".py", ".html", ".js", ".css", ".example"}
    matches = []
    try:
        for root, dirnames, files in os.walk(target_dir):
            # 原地剪枝：跳过依赖/缓存目录，避免爬进 .venv 等海量无关文件
            dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS and not d.startswith(".")]
            for name in files:
                file_path = Path(root) / name
                if Path(name).suffix.lower() not in text_exts:
                    continue
                # 跳过敏感文件，避免把密钥搜出来
                if _is_sensitive(file_path):
                    continue
                try:
                    with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                        for line_no, line in enumerate(f, start=1):
                            if keyword.lower() in line.lower():
                                rel_path = file_path.relative_to(PROJECT_ROOT)
                                matches.append(f"{rel_path}:#{line_no}: {line.strip()}")
                                if len(matches) >= 50:  # 限制返回条数，避免过长
                                    break
                        if len(matches) >= 50:
                            break
                except Exception:
                    continue
            if len(matches) >= 50:
                break
    except Exception as e:
        return f"搜索失败: {e}"

    if not matches:
        return f"未找到包含 '{keyword}' 的内容"
    return "\n".join(matches[:50])


# ---------------- 用户偏好与历史简报 ----------------
# 存储层已迁移到数据库（MySQL / SQLite），见 db.py 与 db/schema_*.sql。
# 这里保留同名函数，让 Agent 工具、API 和定时任务的调用方式保持不变。


def load_preferences() -> Dict[str, Any]:
    """读取用户偏好配置（来自数据库，无记录时返回默认值）。"""
    try:
        return db.load_preferences()
    except Exception as e:
        return {"error": f"读取偏好失败（请检查数据库是否已初始化: python migrate.py）: {e}"}


def save_preferences(preferences: Dict[str, Any]) -> str:
    """保存用户偏好配置到数据库。"""
    try:
        return db.save_preferences(preferences)
    except Exception as e:
        return f"保存偏好失败: {e}"


def load_history() -> List[Dict[str, Any]]:
    """读取历史简报列表（来自数据库，按时间倒序）。"""
    try:
        return db.load_history()
    except Exception:
        return []


def save_history(history: List[Dict[str, Any]]) -> str:
    """
    兼容旧接口。历史记录现在由数据库维护、按行写入，
    因此这里不再整体覆盖保存，仅返回提示。
    """
    return "历史记录由数据库维护，无需整体保存"


def append_to_history(answer: str, auto: bool = False, trace_id: str = "") -> Dict[str, Any]:
    """
    把新生成的简报追加到历史记录，并分离“执行摘要”与“完整 Markdown 正文”。
    手动触发（auto=False）与定时任务（auto=True）共用这一份实现。

    参数:
        answer: Agent 返回的完整回答
        auto: 是否为定时任务产生的记录
        trace_id: 本次 Agent 运行的轨迹 id，可在前端回溯工具调用顺序
    返回:
        本次写入的历史条目
    """
    try:
        return db.append_to_history(answer, auto=auto, trace_id=trace_id)
    except Exception as e:
        return {"error": f"写入历史失败: {e}"}


