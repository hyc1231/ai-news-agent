"""
数据库迁移脚本。

用法：
    python migrate.py                 # 建库建表（按 DB_TYPE 决定 MySQL / SQLite）
    python migrate.py --from-json     # 建表并把旧的 data/*.json 数据导入数据库
    python migrate.py --check         # 只检查连接与表结构是否可用

Windows(PowerShell) 示例：
    $env:DB_TYPE="mysql"; $env:MYSQL_USER="root"; $env:MYSQL_PASSWORD="xxx"
    python migrate.py --from-json
"""

import argparse
import json
import sys
from pathlib import Path

import db
from db import (
    DATA_DIR,
    USE_MYSQL,
    get_conn,
    get_cursor,
    init_db,
    load_history,
    load_preferences,
    save_preferences,
)


def _read_json(name: str):
    path = DATA_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  跳过 {name}：解析失败（{e}）")
        return None


def migrate_from_json() -> int:
    """把旧的 preferences.json / history.json 导入数据库。"""
    imported = 0

    pref = _read_json("preferences.json")
    if isinstance(pref, dict):
        save_preferences(pref)
        print(f"  已导入用户偏好：{pref.get('name', '') or '(未设置称呼)'} / {pref.get('email', '')}")
        imported += 1

    history = _read_json("history.json")
    if isinstance(history, list):
        sql = (
            "INSERT INTO digests (id, digest_date, summary, content, trace_id, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE summary=VALUES(summary)" if USE_MYSQL else
            "INSERT OR REPLACE INTO digests (id, digest_date, summary, content, trace_id, source) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with get_conn() as conn, get_cursor(conn) as cur:
            for item in history:
                cur.execute(
                    sql if USE_MYSQL else sql.replace("%s", "?"),
                    (
                        item.get("id"),
                        item.get("date"),
                        item.get("summary", ""),
                        item.get("content", ""),
                        item.get("trace_id") or None,
                        "scheduled" if item.get("auto") else "manual",
                    ),
                )
                imported += 1
            if not USE_MYSQL:
                conn.commit()
        print(f"  已导入历史简报 {len(history)} 条")

    return imported


def check() -> int:
    """连通性与表结构自检。"""
    print(f"当前数据库后端：{'MySQL' if USE_MYSQL else 'SQLite'}")
    tables = ["preferences", "digests", "tool_calls", "job_runs"]
    with get_conn() as conn, get_cursor(conn) as cur:
        if USE_MYSQL:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (db.MYSQL_DATABASE,),
            )
            existing = {row["TABLE_NAME"] if isinstance(row, dict) else row["table_name"] for row in cur.fetchall()}
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            existing = {row["name"] for row in cur.fetchall()}

    missing = [t for t in tables if t not in existing]
    print(f"  已存在表：{sorted(existing)}")
    if missing:
        print(f"  [缺失] {missing} —— 请执行 python migrate.py")
        return 1

    pref = load_preferences()
    history = load_history()
    print(f"  偏好记录：{pref}")
    print(f"  历史简报：{len(history)} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="每日 AI 新闻助手 - 数据库迁移")
    parser.add_argument("--from-json", action="store_true", help="建表后导入旧的 JSON 数据")
    parser.add_argument("--check", action="store_true", help="只做连通性与表结构自检")
    args = parser.parse_args()

    if args.check:
        return check()

    print("1) 执行建表脚本 ...")
    print("  ", init_db())

    if args.from_json:
        print("2) 从旧 JSON 数据导入 ...")
        count = migrate_from_json()
        print(f"   完成，共导入 {count} 条记录")

    print("3) 自检 ...")
    return check()


if __name__ == "__main__":
    sys.exit(main())
