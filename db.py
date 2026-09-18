"""
数据访问层：同时支持 MySQL 与 SQLite。

环境变量控制：
    DB_TYPE        mysql | sqlite，默认 sqlite（零依赖，便于 5 分钟启动）
    MYSQL_HOST     默认 127.0.0.1
    MYSQL_PORT     默认 3306
    MYSQL_USER     默认 root
    MYSQL_PASSWORD 默认空
    MYSQL_DATABASE 默认 ai_news_agent

占位符统一用 %s 书写，SQLite 分支会自动替换为 ?。
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = PROJECT_ROOT / "db"
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_TYPE = os.getenv("DB_TYPE", "sqlite").strip().lower()
USE_MYSQL = DB_TYPE == "mysql"

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "ai_news_agent")

SQLITE_PATH = DATA_DIR / "agent.db"

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "name": "",
    "email": "",
    "topics": ["人工智能", "大模型", "AI 产品"],
    "keywords": ["OpenAI", "Google", "DeepSeek"],
    "language": "zh",
    "max_articles": 5,
}

_schema_lock = threading.Lock()


def _to_sqlite_sql(sql: str) -> str:
    """把 %s 占位符换成 SQLite 的 ?。"""
    return sql.replace("%s", "?")


@contextmanager
def get_conn():
    """获取数据库连接，用完自动关闭。"""
    if USE_MYSQL:
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_cursor(conn):
    """获取游标：MySQL 需要 dict cursor，SQLite 直接用 conn.cursor。"""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def _split_sql_statements(script: str) -> List[str]:
    """
    把一段 SQL 脚本拆成独立语句。

    需要自己拆的原因：PyMySQL 默认没有开启 CLIENT.MULTI_STATEMENTS 能力位，
    把整段脚本丢给 cur.execute() 会在第一条语句后就报 1064 语法错误，
    所以 MySQL 分支也必须逐条执行，不能依赖驱动的 multi-statement 能力。

    拆分时会跳过字符串/反引号内的分号，并剥掉 -- 行注释与 /* */ 块注释。
    """
    statements: List[str] = []
    buf: List[str] = []
    quote = ""  # 当前所处的引号类型（' " `），空串表示不在引号内
    i = 0
    n = len(script)

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        if quote:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"') and nxt:
                buf.append(nxt)  # 转义字符：连同后一个字符一起吞掉
                i += 2
                continue
            if ch == quote:
                if nxt == quote:
                    buf.append(nxt)  # '' / "" / `` 形式的转义
                    i += 2
                    continue
                quote = ""
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and nxt == "-" and (i + 2 >= n or script[i + 2] in " \t\r\n"):
            while i < n and script[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (script[i] == "*" and script[i + 1] == "/"):
                i += 1
            i += 2
            continue

        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _exec_script(conn, script: str) -> None:
    """执行建表脚本。MySQL 与 SQLite 都拆句后逐条执行，保证两条路径行为一致。"""
    statements = _split_sql_statements(script)

    if USE_MYSQL:
        with get_cursor(conn) as cur:
            for statement in statements:
                cur.execute(statement)
        return

    with conn:
        for statement in statements:
            conn.execute(statement)


def init_db(create_database: bool = True) -> str:
    """
    初始化数据库：执行 db/ 目录下的建表脚本。
    返回描述信息。
    """
    with _schema_lock:
        script = (SCHEMA_DIR / ("schema_mysql.sql" if USE_MYSQL else "schema_sqlite.sql")).read_text(
            encoding="utf-8"
        )

        if USE_MYSQL:
            import pymysql

            if create_database:
                # 先连到实例创建库，再执行后续脚本
                bootstrap = pymysql.connect(
                    host=MYSQL_HOST,
                    port=MYSQL_PORT,
                    user=MYSQL_USER,
                    password=MYSQL_PASSWORD,
                    charset="utf8mb4",
                    autocommit=True,
                )
                try:
                    with bootstrap.cursor() as cur:
                        cur.execute(
                            f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                        )
                finally:
                    bootstrap.close()

            with get_conn() as conn:
                _exec_script(conn, script)
            return f"MySQL 数据表已就绪（{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}）"

        with get_conn() as conn:
            _exec_script(conn, script)
        return f"SQLite 数据表已就绪（{SQLITE_PATH}）"


# ---------------- 偏好 ----------------

def _parse_json_field(value: Any, default: List[str]) -> List[str]:
    """MySQL JSON 字段可能返回 list 或 str，统一解析成 list。"""
    if value is None:
        return default
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else default
    except Exception:
        return default


def load_preferences() -> Dict[str, Any]:
    """读取用户偏好；没有记录时返回默认值。"""
    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(
            "SELECT name, email, topics, keywords, language, max_articles "
            "FROM preferences WHERE id = 1"
        )
        row = cur.fetchone()

    if not row:
        return dict(DEFAULT_PREFERENCES)

    return {
        "name": row["name"] or "",
        "email": row["email"] or "",
        "topics": _parse_json_field(row["topics"], DEFAULT_PREFERENCES["topics"]),
        "keywords": _parse_json_field(row["keywords"], DEFAULT_PREFERENCES["keywords"]),
        "language": row["language"] or "zh",
        "max_articles": row["max_articles"] or 5,
    }


def save_preferences(preferences: Dict[str, Any]) -> str:
    """写入（upsert）用户偏好。"""
    topics = json.dumps(preferences.get("topics", []), ensure_ascii=False)
    keywords = json.dumps(preferences.get("keywords", []), ensure_ascii=False)

    if USE_MYSQL:
        sql = (
            "INSERT INTO preferences (id, name, email, topics, keywords, language, max_articles) "
            "VALUES (1, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name), email=VALUES(email), "
            "topics=VALUES(topics), keywords=VALUES(keywords), "
            "language=VALUES(language), max_articles=VALUES(max_articles)"
        )
    else:
        sql = (
            "INSERT INTO preferences (id, name, email, topics, keywords, language, max_articles, updated_at) "
            "VALUES (1, %s, %s, %s, %s, %s, %s, datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email, "
            "topics=excluded.topics, keywords=excluded.keywords, "
            "language=excluded.language, max_articles=excluded.max_articles, "
            "updated_at=datetime('now','localtime')"
        )

    params = (
        preferences.get("name", ""),
        preferences.get("email", ""),
        topics,
        keywords,
        preferences.get("language", "zh"),
        int(preferences.get("max_articles", 5)),
    )

    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(sql if USE_MYSQL else _to_sqlite_sql(sql), params)
        if not USE_MYSQL:
            conn.commit()
    return "用户偏好已保存"


# ---------------- 历史简报 ----------------

def load_history() -> List[Dict[str, Any]]:
    """读取全部历史简报，按时间倒序。字段对齐旧的 JSON 结构。"""
    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(
            "SELECT id, digest_date, summary, content, trace_id, source, created_at "
            "FROM digests ORDER BY created_at DESC"
        )
        rows = cur.fetchall() or []

    return [
        {
            "id": row["id"],
            "date": str(row["digest_date"]),
            "summary": row["summary"] or "",
            "content": row["content"] or "",
            "trace_id": row["trace_id"] or "",
            "source": row["source"] or "manual",
            "created_at": str(row["created_at"]),
            "auto": (row["source"] == "scheduled"),
        }
        for row in rows
    ]


def append_to_history(answer: str, auto: bool = False, trace_id: str = "") -> Dict[str, Any]:
    """
    追加一条历史简报，自动拆分“执行摘要”和“完整 Markdown 正文”。
    手动触发（auto=False）与定时任务（auto=True）共用这一份实现。
    """
    # id 精确到微秒：旧的秒级 id 在同一秒内会冲突，
    # 冲突时 ON DUPLICATE KEY UPDATE 会静默覆盖上一条记录。
    digest_id = datetime.now().strftime("%Y%m%d%H%M%S%f")

    summary = answer
    content = ""
    if "# " in answer:
        idx = answer.find("# ")
        summary = answer[:idx].strip()
        content = answer[idx:].strip()

    item = {
        "id": digest_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "summary": summary,
        "content": content,
        "trace_id": trace_id,
        "source": "scheduled" if auto else "manual",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    sql = (
        "INSERT INTO digests (id, digest_date, summary, content, trace_id, source) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    params = (digest_id, item["date"], summary, content, trace_id or None, item["source"])

    with get_conn() as conn, get_cursor(conn) as cur:
        if not USE_MYSQL:
            sql = sql + " ON CONFLICT(id) DO UPDATE SET summary=excluded.summary"
        else:
            sql = sql + " ON DUPLICATE KEY UPDATE summary=VALUES(summary)"
        cur.execute(sql if USE_MYSQL else _to_sqlite_sql(sql), params)
        if not USE_MYSQL:
            conn.commit()

    try:
        prune_history()
    except Exception:
        # 清理失败不影响本次写入
        pass
    return item


def prune_history(keep: int = 30) -> None:
    """
    只保留最近 keep 条历史简报，其余删除，
    并同步删除这些简报对应的工具调用轨迹——否则 tool_calls 会无界增长。
    """
    select_sql = (
        "SELECT id, trace_id FROM digests ORDER BY created_at DESC LIMIT %s, 9999" if USE_MYSQL
        else "SELECT id, trace_id FROM digests ORDER BY created_at DESC LIMIT -1 OFFSET %s"
    )
    delete_digest_sql = "DELETE FROM digests WHERE id = %s"
    delete_trace_sql = "DELETE FROM tool_calls WHERE trace_id = %s"

    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(select_sql if USE_MYSQL else _to_sqlite_sql(select_sql), (keep,))
        rows = cur.fetchall() or []
        if not rows:
            return

        cur.executemany(
            delete_digest_sql if USE_MYSQL else _to_sqlite_sql(delete_digest_sql),
            [(row["id"],) for row in rows],
        )
        trace_ids = [row["trace_id"] for row in rows if row["trace_id"]]
        if trace_ids:
            cur.executemany(
                delete_trace_sql if USE_MYSQL else _to_sqlite_sql(delete_trace_sql),
                [(trace_id,) for trace_id in trace_ids],
            )
        if not USE_MYSQL:
            conn.commit()


# ---------------- 定时任务去重 ----------------

def claim_daily_run(name: str, run_date: Optional[str] = None) -> bool:
    """
    抢占「某天某个定时任务」的执行权，抢到返回 True。

    为什么需要：多 worker 部署时（uvicorn --workers N）每个进程都会启动一个
    APScheduler，同一时刻会有 N 个进程同时触发任务，结果一天发出 N 封邮件。
    这里用数据库主键做原子抢占——只有第一个 INSERT 成功的进程才真正执行。
    """
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")

    if USE_MYSQL:
        sql = "INSERT IGNORE INTO job_runs (name, run_date) VALUES (%s, %s)"
    else:
        sql = (
            "INSERT INTO job_runs (name, run_date) VALUES (%s, %s) "
            "ON CONFLICT(name, run_date) DO NOTHING"
        )

    try:
        with get_conn() as conn, get_cursor(conn) as cur:
            cur.execute(sql if USE_MYSQL else _to_sqlite_sql(sql), (name, run_date))
            inserted = cur.rowcount > 0
            if not USE_MYSQL:
                conn.commit()
        return inserted
    except Exception:
        # 抢占失败（含表不存在）时保守放行，避免定时任务彻底停摆
        return True


def get_history_item(digest_id: str) -> Optional[Dict[str, Any]]:
    """按 id 读取单条历史简报。"""
    sql = (
        "SELECT id, digest_date, summary, content, trace_id, source, created_at "
        "FROM digests WHERE id = %s"
    )
    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(sql if USE_MYSQL else _to_sqlite_sql(sql), (digest_id,))
        row = cur.fetchone()

    if not row:
        return None
    return {
        "id": row["id"],
        "date": str(row["digest_date"]),
        "summary": row["summary"] or "",
        "content": row["content"] or "",
        "trace_id": row["trace_id"] or "",
        "source": row["source"] or "manual",
        "created_at": str(row["created_at"]),
    }


# ---------------- Agent 工具调用轨迹 ----------------

def record_trace(trace_id: str, trace: Iterable[Dict[str, Any]]) -> int:
    """
    记录一次 Agent 运行的完整工具调用轨迹。
    返回写入的条数。
    """
    rows = []
    for entry in trace:
        step = entry.get("step", 0)
        for tool_result in entry.get("tool_results", []) or []:
            rows.append((
                trace_id,
                step,
                tool_result.get("name", "unknown"),
                json.dumps(tool_result.get("arguments", {}), ensure_ascii=False)[:2000],
                str(tool_result.get("content", ""))[:2000],
            ))

    if not rows:
        return 0

    sql = "INSERT INTO tool_calls (trace_id, step, tool_name, arguments, result) VALUES (%s, %s, %s, %s, %s)"
    with get_conn() as conn, get_cursor(conn) as cur:
        cur.executemany(sql if USE_MYSQL else _to_sqlite_sql(sql), rows)
        if not USE_MYSQL:
            conn.commit()
    return len(rows)


def get_trace(trace_id: str) -> List[Dict[str, Any]]:
    """按 trace_id 读取工具调用轨迹。"""
    sql = (
        "SELECT step, tool_name, arguments, result, created_at FROM tool_calls "
        "WHERE trace_id = %s ORDER BY step ASC, id ASC"
    )
    with get_conn() as conn, get_cursor(conn) as cur:
        cur.execute(sql if USE_MYSQL else _to_sqlite_sql(sql), (trace_id,))
        rows = cur.fetchall() or []

    return [
        {
            "step": row["step"],
            "tool": row["tool_name"],
            "arguments": row["arguments"],
            "result": row["result"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
