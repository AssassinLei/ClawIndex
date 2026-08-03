"""
ClawIndex 巡检流水线模块

提供统一的巡检执行入口，消除 app.py 与 scheduler.py 中的流水线重复。
调用方只需关心展示/调度逻辑，无需了解数据同步、策略计算、AI生成、结果入库的细节。

两段式拆分：
- prepare_fund_data：同步 + 指标计算，每指数只需执行一次（用户无关）。
- analyze_and_save_for_user：AI 分析 + 入库，按用户执行（读取该用户的提示词配置）。
- run_single_inspection：组合以上两段，供手动巡检（单用户）便捷调用。
"""
from __future__ import annotations

import copy

from data_fetcher import sync_incremental
from strategy_engine import generate_fund_report
from llm_agent import generate_ai_report_with_config
from database import save_inspection_result
from logger import setup_logger

logger = setup_logger("inspection_pipeline")


def prepare_fund_data(fund_code: str, fund_name: str, category: str) -> dict:
    """
    第一段：增量同步 + 指标计算（用户无关，每指数只需执行一次）。

    Returns:
        fund_data dict。同步失败时返回含 "error" 键的 dict，并附加
        "sync_error" 字段（成功时无此字段）。
    """
    # 1. 增量同步
    sync_success, sync_msg = sync_incremental(fund_code)
    if not sync_success:
        return {
            "error": f"数据同步失败: {sync_msg}",
            "fund_code": fund_code,
            "fund_name": fund_name,
            "category": category,
            "sync_error": sync_msg,
        }

    # 2. 指标计算（策略引擎不再输出 decision，只计算指标）
    fund_data = generate_fund_report(fund_code, category)
    fund_data["fund_name"] = fund_name
    fund_data["fund_code"] = fund_code
    fund_data["category"] = category
    return fund_data


def analyze_and_save_for_user(fund_data: dict, username: str, ai_result: dict | None = None) -> dict:
    """
    第二段：AI 分析 + 入库（按用户）。

    参数 ai_result: 已算好的 AI 结果（供定时巡检按配置去重后复用）；
                    为 None 时按该用户的提示词配置调用一次 AI。

    对 fund_data 做浅拷贝再写入 decision，避免多用户共享同一 dict 互相覆盖。
    has_error 时 decision.action 与入库 action 统一为"数据异常"。
    ai_result 含 ai_error 标记（AI 调用失败）时不入库，避免垃圾结果覆盖当日有效记录。

    Returns:
        {
            "fund_data": dict,   # 含该用户 decision 的用户版 fund_data
            "ai_report": dict,   # {analysis, advice, confidence[, ai_error]}
            "has_error": bool,   # 策略引擎/同步是否返回 error
            "ai_error": bool,    # AI 调用是否失败（失败时未入库）
        }
    """
    has_error = "error" in fund_data

    # 3. AI 分析（返回 dict: {analysis, advice, confidence}）
    if ai_result is None:
        ai_result = generate_ai_report_with_config(fund_data, username)
    advice = ai_result.get("advice", "持有/观望")
    analysis = ai_result.get("analysis", "")
    confidence = ai_result.get("confidence", 0)
    ai_error = ai_result.get("ai_error", False)

    # 统一 action：错误分支下 DB、UI 卡片、webhook 推送三方一致
    final_action = "数据异常" if has_error else advice

    # 浅拷贝一份用户专属 fund_data，构建兼容的 decision 结构供下游（app/webhook）消费
    user_fund_data = copy.copy(fund_data)
    user_fund_data["decision"] = {
        "action": final_action,
        "details": [analysis],
    }

    # 4. 入库（按用户）；AI 调用失败时跳过，避免 INSERT OR REPLACE 覆盖当日已有有效记录
    if ai_error:
        logger.warning(
            f"analyze_and_save_for_user: [{username}] {fund_data['fund_code']} "
            f"AI 调用失败，跳过入库: {analysis[:100]}"
        )
    else:
        inds = user_fund_data.get("indicators", {})
        save_inspection_result(
            username, fund_data["fund_code"], fund_data.get("fund_name", fund_data["fund_code"]),
            final_action,
            analysis,
            pe_percentile=inds.get("pe_percentile"),
            pb_percentile=inds.get("pb_percentile"),
            ma60=inds.get("ma60"),
            ma120=inds.get("ma120"),
            amount=inds.get("amount"),
            amount_ma20=inds.get("amount_ma20"),
            confidence=confidence,
        )

    return {
        "fund_data": user_fund_data,
        "ai_report": ai_result,
        "has_error": has_error,
        "ai_error": ai_error,
    }


def run_single_inspection(fund_code: str, fund_name: str, category: str, username: str) -> dict:
    """
    执行单个标的针对单个用户的完整巡检（同步 → 指标计算 → AI → 入库）。
    供 app.py 手动巡检调用。同步失败时不入库，直接返回错误结果。

    Returns:
        {
            "fund_data": dict,       # 含 fund_name / decision 字段
            "ai_report": dict,       # AI 结构化输出 {analysis, advice, confidence[, ai_error]}
            "has_error": bool,       # 策略引擎/同步是否返回 error
            "ai_error": bool,        # AI 调用是否失败（失败时未入库）；同步失败分支无此键，消费方用 .get 兜底
            "sync_error": str | None,# 同步失败时的错误信息（成功时为 None）
        }
    """
    fund_data = prepare_fund_data(fund_code, fund_name, category)
    sync_error = fund_data.pop("sync_error", None)

    if sync_error:
        # 同步失败：不入库（避免 INSERT OR REPLACE 覆盖当日已有有效记录），仅返回错误供 UI 展示
        fund_data["decision"] = {"action": "数据异常", "details": [fund_data["error"]]}
        return {
            "fund_data": fund_data,
            "ai_report": {"analysis": f"数据获取异常：{fund_data['error']}", "advice": "持有/观望", "confidence": 0},
            "has_error": True,
            "sync_error": sync_error,
        }

    result = analyze_and_save_for_user(fund_data, username)
    result["sync_error"] = None
    return result
