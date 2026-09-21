"""
全项目「今天」的时间基准是否唯一。

回归背景：以前判「今天」有两套基准 —— `tools/search_tools.py` 用 `TIMEZONE`
（默认 Asia/Shanghai）把 UTC 发布时间换算过来，而 `db.py` / `agent.py` /
`digest_tools.py` / `app.py` / `scheduler.py` 各自用裸 `datetime.now()`，
跟着**服务器本地时区**走。开发机上两者恰好一致，换到 UTC 云主机就会出现
「新闻按北京日期筛选、简报的日期字段却按 UTC 写入」，跨零点那几小时整整差一天。

数据库层的三个默认值（SQLite 的 `datetime('now','localtime')`、MySQL 的
`CURRENT_TIMESTAMP`）是同一个坑的第三种来源，所以现在四条写库语句都显式带上时间列。

本文件守着这两条约束，防止基准再次分叉。
"""

import pathlib
from datetime import datetime, timedelta

import agent
import app as app_module  # noqa: F401  能被导入即说明没引用已删除的符号
import clock
import db
import scheduler  # noqa: F401
import tools.digest_tools as digest_tools
import tools.search_tools as search_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]

BUSINESS_MODULES = (
    "db.py",
    "agent.py",
    "app.py",
    "scheduler.py",
    "tools/search_tools.py",
    "tools/digest_tools.py",
    "tools/file_tools.py",
    "tools/email_tools.py",
)


def _code_lines(rel_path: str):
    """去掉空行与整行注释后的代码行（注释里提到旧写法是正常的）。"""
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --- 唯一基准 ---------------------------------------------------------------


def test_search_tools_reuses_clock_timezone():
    """search_tools 不再自持一份时区解析，用的就是 clock 里那个对象。"""
    assert search_tools._LOCAL_TZ is clock.LOCAL_TZ


def test_search_tools_has_no_private_timezone_settings():
    """旧的 NEWS_TIMEZONE / _resolve_tz 必须删干净，留着就是第二套基准。"""
    assert not hasattr(search_tools, "NEWS_TIMEZONE")
    assert not hasattr(search_tools, "_resolve_tz")


def test_local_today_is_shared():
    assert search_tools.local_today() == clock.local_today()


def test_db_and_digest_borrow_clock_functions():
    assert db.local_now is clock.local_now
    assert db.local_stamp is clock.local_stamp
    assert agent.local_stamp is clock.local_stamp
    assert digest_tools.local_stamp is clock.local_stamp


def test_no_bare_datetime_now_left():
    """业务代码里不允许再出现裸 datetime.now()——它跟服务器时区走。"""
    offenders = [
        rel
        for rel in BUSINESS_MODULES
        if any("datetime.now()" in line for line in _code_lines(rel))
    ]
    assert offenders == [], f"这些文件仍在用裸 datetime.now()，应改用 clock：{offenders}"


# --- 时区确实按 TIMEZONE 解析 -----------------------------------------------


def test_timezone_name_is_honored(monkeypatch):
    monkeypatch.setattr(clock, "TIMEZONE_NAME", "Pacific/Midway")

    # 必须给一个真实 datetime：ZoneInfo.utcoffset(None) 返回 None（它要按具体时刻算偏移）
    assert clock._resolve_tz().utcoffset(datetime(2026, 1, 1)) == timedelta(hours=-11)


def test_unknown_timezone_falls_back_to_utc8(monkeypatch):
    """时区名写错时回退到固定 UTC+8，不能让整条链路挂掉。"""
    monkeypatch.setattr(clock, "TIMEZONE_NAME", "Not/A-Timezone")

    assert clock._resolve_tz().utcoffset(datetime(2026, 1, 1)) == timedelta(hours=8)


# --- 写库时间戳的格式与来源 -------------------------------------------------


def test_datetime_format_avoids_iso_separator():
    """created_at 用空格分隔：digests 按它做字符串排序，混用 "T" 会让排序错乱。"""
    assert clock.DATETIME_FORMAT == "%Y-%m-%d %H:%M:%S"
    assert "T" not in clock.local_stamp(clock.DATETIME_FORMAT)
    assert clock.local_stamp() == clock.local_today().strftime(clock.DATE_FORMAT)


def test_insert_statements_carry_explicit_timestamps():
    """
    四条写库语句都必须显式带上时间列。

    不显式写就会回落到数据库默认值（SQLite 的 datetime('now','localtime')、
    MySQL 的 CURRENT_TIMESTAMP），那是「第三套基准」，换到 UTC 主机同样错位。
    """
    sql_lines = " ".join(_code_lines("db.py"))
    for needle in (
        "INSERT INTO preferences (id, name, email, topics, keywords, language, max_articles, updated_at)",
        "INSERT INTO digests (id, digest_date, summary, content, trace_id, source, created_at)",
        "INSERT INTO job_runs (name, run_date, started_at)",
        "INSERT INTO tool_calls (trace_id, step, tool_name, arguments, result, created_at)",
    ):
        assert needle in sql_lines, f"写库语句未显式写入时间列: {needle}"
