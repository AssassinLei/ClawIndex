"""
ClawIndex 定时调度模块
- 每个交易日 19:30 自动执行巡检
- 复用现有流水线：同步 → 策略 → AI → 入库 → Webhook
- 交易日历二次确认，跳过节假日
"""
from __future__ import annotations

import threading
from datetime import datetime
from logger import setup_logger

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import (
    get_distinct_funds, get_fund_watchers_map, get_all_users,
    get_webhook_urls, get_setting, get_user_setting,
)
from data_fetcher import is_trade_day
from inspection_pipeline import run_single_inspection
from webhook_sender import send_to_all_webhooks

logger = setup_logger("scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()

_WDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def run_scheduled_inspection():
    """
    执行一轮完整巡检（无 Streamlit 依赖）。
    与 app.py 手动巡检共享同一流水线。
    """
    with _lock:
        # 二次检查：调度器可能被错误启动，此处确保仅在开关开启时执行
        if get_setting("scheduler_auto_enabled", "true") != "true":
            logger.info("定时巡检已关闭（scheduler_auto_enabled != true），本轮任务跳过")
            return

        logger.info("=== 定时巡检开始 ===")
        # 全体用户基金池并集去重：同一指数每天只巡检一次
        funds = get_distinct_funds()
        if not funds:
            logger.info("监控池为空，跳过定时巡检")
            return

        total = len(funds)
        success_count = 0
        sync_error_count = 0
        pipeline_error_count = 0

        # 循环外一次性构建推送路由映射，避免每只标的重复查库：
        # watchers_map: fund_code → 监控用户列表
        # user_webhooks: 开启推送的用户 → 其 webhook 列表
        watchers_map = get_fund_watchers_map()
        user_webhooks: dict[str, list] = {}
        for user in get_all_users():
            if get_user_setting(user, "webhook_inspection_enabled", "false") == "true":
                user_webhooks[user] = get_webhook_urls(user)

        for fund in funds:
            code = fund["fund_code"]
            name = fund["fund_name"]
            cat = fund["category"]
            logger.info(f"定时巡检: [{success_count + sync_error_count + pipeline_error_count + 1}/{total}] {name} ({code})")

            try:
                result = run_single_inspection(code, name, cat)

                if result["sync_error"]:
                    logger.warning(f"定时巡检: {name} 同步失败 - {result['sync_error']}")
                    sync_error_count += 1
                    continue

                fund_data = result["fund_data"]
                ai_report = result["ai_report"]

                # 5. Webhook 推送：只推给监控该指数且开启推送的用户（按 url 去重）
                target_whs = []
                seen_urls = set()
                for user in watchers_map.get(code, []):
                    for wh in user_webhooks.get(user, []):
                        wh_url = wh.get("url", "")
                        if wh_url and wh_url not in seen_urls:
                            seen_urls.add(wh_url)
                            target_whs.append(wh)
                if target_whs:
                    send_to_all_webhooks(target_whs, fund_data)

                success_count += 1

            except Exception as e:
                logger.error(f"定时巡检: {name} ({code}) 处理异常: {e}，其他标的将继续巡检", exc_info=True)
                pipeline_error_count += 1
                continue

        error_count = sync_error_count + pipeline_error_count
        logger.info(
            f"=== 定时巡检结束 === 成功 {success_count}, 失败/跳过 {error_count}"
            f" (同步失败 {sync_error_count}, 流水线异常 {pipeline_error_count})"
        )


def _should_run_today() -> bool:
    """检查今天是否为交易日（二次确认，跳过法定节假日）"""
    today = datetime.now().strftime("%Y%m%d")
    is_open, _ = is_trade_day(today)
    if not is_open:
        weekday = datetime.now().weekday()
        logger.info(f"今天 {today}({_WDAY_NAMES[weekday]}) 为非交易日，跳过定时巡检")
    return is_open


def start_scheduler():
    """
    启动定时调度器（仅首次调用生效，防重复）。
    每个交易日 19:30 执行。
    """
    global _scheduler

    if _scheduler is not None:
        return  # 已启动

    _scheduler = BackgroundScheduler(
        daemon=True,
        timezone="Asia/Shanghai",
    )

    # 周一到周五 19:30，执行前走交易日历二次确认
    _scheduler.add_job(
        func=lambda: _should_run_today() and run_scheduled_inspection(),
        trigger=CronTrigger(day_of_week="mon-fri", hour=19, minute=30),
        id="clawindex_daily_inspection",
        name="ClawIndex 每日巡检",
        max_instances=1,          # 禁止并发
        coalesce=True,             # 积压时合并执行
    )

    _scheduler.start()
    logger.info("定时调度器已启动: 每个交易日 19:30 自动巡检")


def stop_scheduler():
    """关闭定时调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度器已停止")


def get_next_run_time() -> str | None:
    """获取下次执行时间，用于侧边栏展示"""
    if _scheduler is None:
        return None
    try:
        job = _scheduler.get_job("clawindex_daily_inspection")
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return None


def is_scheduler_running() -> bool:
    """检查调度器是否在运行"""
    return _scheduler is not None and _scheduler.running
