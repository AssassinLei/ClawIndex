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

from database import get_all_funds, save_inspection_result, get_webhook_urls, get_setting
from data_fetcher import sync_incremental, is_trade_day
from strategy_engine import generate_fund_report
from llm_agent import generate_ai_report
from webhook_sender import send_to_all_webhooks

logger = setup_logger("scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def run_scheduled_inspection():
    """
    执行一轮完整巡检（无 Streamlit 依赖）。
    与 app.py 手动巡检共享同一流水线。
    """
    with _lock:
        logger.info("=== 定时巡检开始 ===")
        funds = get_all_funds()
        if not funds:
            logger.info("监控池为空，跳过定时巡检")
            return

        total = len(funds)
        success_count = 0
        sync_error_count = 0
        pipeline_error_count = 0

        for fund in funds:
            code = fund["fund_code"]
            name = fund["fund_name"]
            cat = fund["category"]
            logger.info(f"定时巡检: [{success_count + sync_error_count + pipeline_error_count + 1}/{total}] {name} ({code})")

            try:
                # 1. 增量同步
                sync_success, sync_msg = sync_incremental(code)
                if not sync_success:
                    logger.warning(f"定时巡检: {name} 同步失败 - {sync_msg}")
                    sync_error_count += 1
                    continue

                # 2. 策略引擎
                fund_data = generate_fund_report(code, cat)
                fund_data["fund_name"] = name

                # 3. AI 报告
                ai_report = generate_ai_report(fund_data)

                # 4. 保存结果（含计算指标）
                has_error = "error" in fund_data
                action = fund_data.get("decision", {}).get("action", "ERROR")
                inds = fund_data.get("indicators", {})
                save_inspection_result(
                    code, name,
                    action if not has_error else "ERROR",
                    ai_report,
                    pe_percentile=inds.get("pe_percentile"),
                    pb_percentile=inds.get("pb_percentile"),
                    ma60=inds.get("ma60"),
                    ma120=inds.get("ma120"),
                )

                # 5. Webhook 推送（尊重用户开关设置）
                webhook_enabled = get_setting("webhook_inspection_enabled", "false")
                if webhook_enabled == "true":
                    wh_urls = get_webhook_urls()
                    if wh_urls:
                        send_to_all_webhooks(wh_urls, fund_data, ai_report)

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
        wday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        logger.info(f"今天 {today}({wday_names[weekday]}) 为非交易日，跳过定时巡检")
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
