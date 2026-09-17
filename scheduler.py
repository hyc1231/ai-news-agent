"""
定时任务调度器：每天固定时间自动为用户生成并发送新闻简报。
"""

import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from agent import generate_and_send_digest
from tools.file_tools import append_to_history

load_dotenv()


# 从环境变量读取调度配置，默认每天早上 8 点
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")


def _parse_time(time_str: str):
    """把 HH:MM 格式解析成小时和分钟。"""
    try:
        hour, minute = time_str.strip().split(":")
        return int(hour), int(minute)
    except Exception:
        return 8, 0


def run_scheduled_digest():
    """
    定时任务入口：调用 Agent 生成并发送简报，并把结果写入历史。
    """
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
    hour, minute = _parse_time(SCHEDULE_TIME)

    scheduler.add_job(
        run_scheduled_digest,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="daily_digest_job",
        name="每日新闻简报",
        replace_existing=True,
    )
    return scheduler


# 全局调度器实例
_scheduler = get_scheduler()


def start_scheduler():
    """启动调度器。"""
    if not _scheduler.running:
        _scheduler.start()
        print(f"定时任务已启动：每天 {SCHEDULE_TIME}（{TIMEZONE}）自动生成简报")


def shutdown_scheduler():
    """关闭调度器。"""
    if _scheduler.running:
        _scheduler.shutdown()
        print("定时任务已关闭")


@asynccontextmanager
async def lifespan(app) -> AsyncGenerator[None, None]:
    """FastAPI 生命周期管理：启动时开启调度器，关闭时停止。"""
    start_scheduler()
    yield
    shutdown_scheduler()
