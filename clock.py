"""
全项目统一的「本地时间」基准。

为什么需要这个模块：
以前判「今天」有两套基准 —— `tools/search_tools.py` 用 `TIMEZONE`（默认 Asia/Shanghai）
把 UTC 的发布时间换算过来，而 `db.py` / `agent.py` / `digest_tools.py` / `app.py` / `scheduler.py`
各自用裸 `datetime.now()`，跟着**服务器本地时区**走。

在开发机上两者恰好都是 Asia/Shanghai，所以看不出问题；一旦部署到 UTC 服务器
（云主机默认就是 UTC），就会出现「新闻按北京日期筛选、简报的日期字段却按 UTC 写入」，
跨零点那几小时会整整差一天。数据库层的三个默认值（SQLite 的 `datetime('now','localtime')`、
MySQL 的 `CURRENT_TIMESTAMP`）也是同样的坑，所以现在所有写入都改为显式传时间戳，
不再依赖数据库服务器或操作系统的时区设置。

唯一的时间来源就是这里的 `TIMEZONE`，改一处即全项目一致。
"""

import os
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

try:  # Python 3.9+ 标准库；Windows 上依赖 tzdata 提供时区库
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# 允许 .env 提供 TIMEZONE。load_dotenv 默认**不覆盖**已存在的环境变量，
# 所以测试里把配置预设成空串的做法依然有效，不会被真实的 .env 顶掉。
load_dotenv()

TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Shanghai")

# 写进数据库的日期/时间格式。刻意不用 isoformat()：
# 它用 "T" 分隔，和历史数据（以及 SQLite 的 datetime()）不一致，
# 而 digests 是按 created_at 字符串排序的 —— 混用两种分隔符会让排序结果错乱
# （"T" 的字符码大于空格，同一天的旧数据会被排到新数据后面）。
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_tz():
    """解析时区对象，失败时回退到固定 UTC+8，保证功能不中断。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


LOCAL_TZ = _resolve_tz()


def local_now() -> datetime:
    """当前用户所在时区的「现在」，带时区信息。"""
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    """当前用户所在时区的「今天」。"""
    return local_now().date()


def local_stamp(fmt: str = DATE_FORMAT) -> str:
    """当前用户所在时区的「现在」按指定格式输出，供写库与日志使用。"""
    return local_now().strftime(fmt)
