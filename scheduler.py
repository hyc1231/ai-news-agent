"""
定时任务调度器：每天固定时间自动为用户生成并发送新闻简报。

多 worker 说明：uvicorn --workers N 时每个进程都会启动一个调度器，
同一时刻会有 N 个进程同时触发任务。run_scheduled_digest 用数据库
job_runs 表做主键抢占，保证当天只有第一个进程真正执行，不会重复发邮件。
"""

import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import db
from agent import generate_and_send_digest
from tools.file_tools import append_to_history

load_dotenv()


# 从环境变量读取调度配置，默认每天早上 8 点
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")

# 每日任务的去重标识
DAILY_JOB_NAME = "daily_digest"

# 是否在当前进程内启用调度器。多 worker 部署时建议设为 false，
# 改为单独跑一个 `python scheduler.py` 进程。
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").strip().lower() not in ("0", "false", "no")


def _parse_time(time_str: str):
    """
    把 HH:MM 格式解析成小时和分钟；无法解析或越界时回落到默认的 08:00。

    为什么越界值也必须在这里兜住：本函数在**模块级**被调用（见文件末尾的
    `_scheduler = get_scheduler()`），一旦上抛异常，`from scheduler import lifespan`
    就会失败，进而整个 app 无法导入、服务完全起不来。而那个 traceback 会一路穿过
    uvicorn / click / importlib，最外层信息完全不指向 SCHEDULE_TIME，用户极难定位。
    所以宁可回落到默认时间并打一行警告，也要保证服务能正常启动。
    """
    try:
        hour, minute = time_str.strip().split(":")
        hour, minute = int(hour), int(minute)
    except Exception:
        print(f"[警告] SCHEDULE_TIME={time_str!r} 无法解析为 HH:MM，已回落到 08:00")
        return 8, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        print(
            f"[警告] SCHEDULE_TIME={time_str!r} 超出范围"
            f"（小时 0-23、分钟 0-59），已回落到 08:00"
        )
        return 8, 0
    return hour, minute


# 模块级解析一次，之后全程复用这两个值。
# 原因：_parse_time 在配置非法时会打印回落警告，若每个使用点各自解析一遍，
# 同一条警告会在启动日志里重复出现，看起来像是多个配置项都写错了。
SCHEDULE_HOUR, SCHEDULE_MINUTE = _parse_time(SCHEDULE_TIME)


def run_scheduled_digest(force: bool = False) -> None:
    """
    定时任务入口：调用 Agent 生成并发送简报，并把结果写入历史。

    会先用数据库抢占「今天这个任务」的执行权（force=True 可跳过），
    避免多 worker 部署时同一天重复生成、重复发送。
    """
    if not force and not db.claim_daily_run(DAILY_JOB_NAME):
        print(f"[{datetime.now().isoformat()}] 今日定时简报任务已由其它进程执行过，本次跳过")
        return

    print(f"[{datetime.now().isoformat()}] 开始执行定时简报任务...")
    trace_id = f"sched-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    result = generate_and_send_digest(trace_id=trace_id)

    if result.get("success"):
        # 历史写入逻辑统一由 tools.file_tools 维护
        append_to_history(result.get("answer", ""), auto=True, trace_id=trace_id)
        print(f"[{datetime.now().isoformat()}] 定时简报任务完成")
    else:
        print(f"[{datetime.now().isoformat()}] 定时简报任务失败: {result.get('answer')}")


def get_scheduler() -> BackgroundScheduler:
    """创建并配置后台调度器。"""
    scheduler = BackgroundScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        run_scheduled_digest,
        trigger=CronTrigger(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE),
        id="daily_digest_job",
        name="每日新闻简报",
        replace_existing=True,
    )
    return scheduler


# 全局调度器实例
_scheduler = get_scheduler()


def start_scheduler():
    """启动调度器。"""
    if not ENABLE_SCHEDULER:
        print("定时任务已通过 ENABLE_SCHEDULER=false 关闭（可由独立进程 python scheduler.py 承担）")
        return
    if not _scheduler.running:
        _scheduler.start()
        # 打印**实际生效**的时间（而非 .env 里的原始字符串）：
        # 配置越界时解析结果会回落到 08:00，若直接回显原始值，
        # 日志就会与上一行的回落警告自相矛盾。
        print(
            f"定时任务已启动：每天 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}"
            f"（{TIMEZONE}）自动生成简报"
        )


def shutdown_scheduler():
    """关闭调度器。"""
    if _scheduler.running:
        _scheduler.shutdown()
        print("定时任务已关闭")


def main() -> None:
    """
    以独立进程运行调度器。
    多 worker 部署时建议：API 进程设 ENABLE_SCHEDULER=false，
    另外单独起一个 `python scheduler.py` 专管定时任务。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    blocking = BlockingScheduler(timezone=TIMEZONE)
    blocking.add_job(
        run_scheduled_digest,
        trigger=CronTrigger(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE),
        id="daily_digest_job",
        name="每日新闻简报",
        replace_existing=True,
    )
    print(
        f"定时任务已启动：每天 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}"
        f"（{TIMEZONE}），Ctrl+C 退出"
    )
    try:
        blocking.start()
    except (KeyboardInterrupt, SystemExit):
        print("定时任务已停止")


if __name__ == "__main__":
    main()


@asynccontextmanager
async def lifespan(app) -> AsyncGenerator[None, None]:
    """FastAPI 生命周期管理：启动时开启调度器，关闭时停止。"""
    start_scheduler()
    yield
    shutdown_scheduler()
