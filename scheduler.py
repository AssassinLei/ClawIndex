"""
ClawIndex 定时调度模块
- 按全局配置的 Cron 表达式自动执行巡检（默认 30 19 * * 1-5 = 工作日 19:30）
- 复用现有流水线：同步 → 策略 → AI → 入库 → Webhook
- 交易日历二次确认，跳过节假日
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from logger import setup_logger

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import (
    get_distinct_funds, get_fund_watchers_map, get_all_users,
    get_webhook_urls, get_setting, set_setting, get_user_setting,
    get_prompt_configs_for_users,
)
from data_fetcher import is_trade_day
from inspection_pipeline import prepare_fund_data, analyze_and_save_for_user
from llm_agent import generate_ai_report, DEFAULT_INDICATORS
from webhook_sender import send_to_all_webhooks, is_action_pushable

logger = setup_logger("scheduler")

# 相邻两次真实 LLM 调用的间隔秒数（防限流，与 data_fetcher 的 Tushare 节流惯例同量级）
LLM_CALL_INTERVAL = 1.0

DEFAULT_CRON_EXPR = "30 19 * * 1-5"   # 等价原"工作日 19:30"（标准 cron 周字段 1-5=周一~周五）
CRON_SETTING_KEY = "scheduler_cron_expr"


def parse_cron_expr(expr: str) -> CronTrigger | None:
    """解析标准 5 字段 cron 表达式；非法返回 None（不抛异常，便于调用方与 UI 复用）"""
    try:
        return CronTrigger.from_crontab(expr.strip(), timezone="Asia/Shanghai")
    except (ValueError, TypeError):
        return None

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
        ai_fail_count = 0  # AI 调用失败而跳过入库/推送的用户次数

        # 循环外一次性构建推送路由映射，避免每只标的重复查库：
        # watchers_map: fund_code → 监控用户列表
        # user_webhooks: 开启推送的用户 → 其 webhook 列表
        watchers_map = get_fund_watchers_map()
        user_webhooks: dict[str, list] = {}
        user_action_filter: dict[str, bool] = {}   # 需求1: 用户级过滤开关, 预构建读取, 热循环零 DB 开销
        for user in get_all_users():
            if get_user_setting(user, "webhook_inspection_enabled", "false") == "true":
                user_webhooks[user] = get_webhook_urls(user)
                user_action_filter[user] = get_user_setting(user, "webhook_strong_signal_only", "true") == "true"

        for fund in funds:
            code = fund["fund_code"]
            name = fund["fund_name"]
            cat = fund["category"]
            logger.info(f"定时巡检: [{success_count + sync_error_count + pipeline_error_count + 1}/{total}] {name} ({code})")

            try:
                # 1. 同步 + 指标计算（每指数只执行一次）
                fund_data = prepare_fund_data(code, name, cat)
                sync_error = fund_data.pop("sync_error", None)
                if sync_error:
                    logger.warning(f"定时巡检: {name} 同步失败 - {sync_error}")
                    sync_error_count += 1
                    continue

                # 2. 取监控该指数的用户，按提示词配置分组去重（相同配置只调一次 LLM）
                watchers = watchers_map.get(code, [])
                configs = get_prompt_configs_for_users(code, watchers)
                # 签名 (custom_prompt, tuple(sorted(归一化后指标))) → AI 结果缓存；
                # 未配置与显式勾选默认指标的用户归一化后合并为同一组，避免等效配置重复调用
                group_cache: dict[tuple, dict] = {}
                llm_calls = 0
                # 每指数内已推送的 URL 集合：同一 URL 被多用户配置时只推一次（先到先得）
                pushed_urls: set[str] = set()

                for user in watchers:
                    custom_prompt, selected_indicators = configs.get(user, (None, []))
                    # 归一化：空配置与显式勾选默认指标的 Prompt 完全一致，统一按默认指标处理
                    effective_inds = selected_indicators or DEFAULT_INDICATORS
                    signature = (custom_prompt, tuple(sorted(effective_inds)))
                    ai_result = group_cache.get(signature)
                    if ai_result is None:
                        ai_result = generate_ai_report(
                            fund_data,
                            custom_prompt=custom_prompt,
                            selected_indicators=effective_inds,
                        )
                        # 失败结果（含 ai_error）也写入缓存：同组后续用户不再重复触发注定失败的调用
                        group_cache[signature] = ai_result
                        llm_calls += 1
                        # 防 LLM 限流：仅真实调用后休眠，缓存命中零开销
                        time.sleep(LLM_CALL_INTERVAL)

                    # 3. 每个用户各自入库（复用同组 AI 结果；AI 失败时 pipeline 层已跳过入库）
                    user_result = analyze_and_save_for_user(fund_data, user, ai_result=ai_result)

                    # AI 调用失败：不推送垃圾结果，计数后处理下一用户
                    if user_result.get("ai_error"):
                        logger.warning(f"定时巡检: {code} [{user}] AI 调用失败，跳过推送")
                        ai_fail_count += 1
                        continue

                    # 需求1: 仅推送买入/卖出强信号（用户级开关，默认开启）。必须置于 pushed_urls 去重之前：
                    # 被过滤用户不占用 URL 去重名额，避免同 URL 其他用户的买入/卖出信号被误跳过
                    if user_action_filter.get(user, True) and not is_action_pushable(user_result["fund_data"]):
                        logger.info(f"定时巡检: {code} [{user}] 建议非买入/卖出，按过滤开关跳过推送")
                        continue

                    # 4. 仅向该用户自己的 webhook 推送其专属结果（跨用户 URL 去重）
                    target_whs = []
                    for wh in user_webhooks.get(user, []):
                        wh_url = wh.get("url", "")
                        if not wh_url:
                            continue
                        if wh_url in pushed_urls:
                            logger.info(f"定时巡检: {code} [{user}] 的 webhook 已由其他用户推送过，跳过去重")
                            continue
                        pushed_urls.add(wh_url)
                        target_whs.append(wh)
                    if target_whs:
                        send_to_all_webhooks(target_whs, user_result["fund_data"])

                logger.info(
                    f"定时巡检: {name} ({code}) 完成 - 监控用户 {len(watchers)} 个, "
                    f"不同配置 {len(group_cache)} 组, LLM 调用 {llm_calls} 次"
                )
                success_count += 1

            except Exception as e:
                logger.error(f"定时巡检: {name} ({code}) 处理异常: {e}，其他标的将继续巡检", exc_info=True)
                pipeline_error_count += 1
                continue

        error_count = sync_error_count + pipeline_error_count
        summary = (
            f"=== 定时巡检结束 === 成功 {success_count}, 失败/跳过 {error_count}"
            f" (同步失败 {sync_error_count}, 流水线异常 {pipeline_error_count}, AI 失败 {ai_fail_count} 用户次)"
        )
        if ai_fail_count > 0:
            logger.warning(summary + " —— 存在 AI 调用失败，相关用户当日未入库/推送，请检查 API 状态")
        else:
            logger.info(summary)


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
    按全局 Cron 表达式调度，执行前走交易日历二次确认。
    """
    global _scheduler

    with _lock:
        if _scheduler is not None:
            return  # 已启动

        _scheduler = BackgroundScheduler(
            daemon=True,
            timezone="Asia/Shanghai",
        )

        cron_expr = get_setting(CRON_SETTING_KEY, DEFAULT_CRON_EXPR)
        trigger = parse_cron_expr(cron_expr)
        if trigger is None:
            # 防御性回退：数据库被手改坏时保证调度器永不因坏配置挂起
            logger.warning(f"scheduler: cron 表达式无效 ({cron_expr!r})，回退默认 {DEFAULT_CRON_EXPR}")
            trigger = CronTrigger.from_crontab(DEFAULT_CRON_EXPR, timezone="Asia/Shanghai")

        _scheduler.add_job(
            func=lambda: _should_run_today() and run_scheduled_inspection(),
            trigger=trigger,
            id="clawindex_daily_inspection",
            name="ClawIndex 每日巡检",
            max_instances=1,          # 禁止并发
            coalesce=True,             # 积压时合并执行
        )

        _scheduler.start()
        logger.info(f"定时调度器已启动: cron={cron_expr}（执行前仍走交易日历二次确认）")


def stop_scheduler():
    """关闭定时调度器"""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
            logger.info("定时调度器已停止")


def update_cron_expression(expr: str) -> tuple[bool, str]:
    """校验并应用新的调度 cron 表达式；非法时返回 (False, 错误信息)，不落库、不影响现有调度。

    注意：本函数不持锁（Lock 非可重入），通过 stop/start 各自内部加锁保证安全。
    """
    expr = expr.strip()
    if parse_cron_expr(expr) is None:
        return False, f"Cron 表达式无效（示例：30 19 * * 1-5）：{expr!r}"
    set_setting(CRON_SETTING_KEY, expr)
    if _scheduler is not None:   # 仅运行中重启；未运行则下次开启时生效
        stop_scheduler()
        start_scheduler()
    return True, f"调度时间已更新为 {expr}（{'已生效' if _scheduler is not None else '调度器未运行，将在开启后生效'}）"


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
