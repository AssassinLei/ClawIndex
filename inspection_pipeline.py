"""
ClawIndex 巡检流水线模块

提供统一的巡检执行入口，消除 app.py 与 scheduler.py 中的流水线重复。
调用方只需关心展示/调度逻辑，无需了解数据同步、策略计算、AI生成、结果入库的细节。
"""
from __future__ import annotations

from data_fetcher import sync_incremental
from strategy_engine import generate_fund_report
from llm_agent import generate_ai_report_with_config
from database import save_inspection_result
from logger import setup_logger

logger = setup_logger("inspection_pipeline")


def run_single_inspection(fund_code: str, fund_name: str, category: str) -> dict:
    """
    执行单个标的完整巡检（同步 → 指标计算 → AI → 入库）。

    Returns:
        {
            "fund_data": dict,       # generate_fund_report 的返回结果（含 fund_name 字段）
            "ai_report": dict,       # AI 结构化输出 {analysis, advice, confidence}
            "has_error": bool,       # 策略引擎是否返回 error
            "sync_error": str | None,# 同步失败时的错误信息（成功时为 None）
        }
    """
    # 1. 增量同步
    sync_success, sync_msg = sync_incremental(fund_code)
    if not sync_success:
        fund_data = {
            "error": f"数据同步失败: {sync_msg}",
            "fund_code": fund_code,
            "fund_name": fund_name,
            "category": category,
        }
        ai_report = generate_ai_report_with_config(fund_data)
        return {
            "fund_data": fund_data,
            "ai_report": ai_report,
            "has_error": True,
            "sync_error": sync_msg,
        }

    # 2. 指标计算（策略引擎不再输出 decision，只计算指标）
    fund_data = generate_fund_report(fund_code, category)
    fund_data["fund_name"] = fund_name
    fund_data["fund_code"] = fund_code
    fund_data["category"] = category

    has_error = "error" in fund_data

    # 3. AI 分析（返回 dict: {analysis, advice, confidence}）
    ai_result = generate_ai_report_with_config(fund_data)
    advice = ai_result.get("advice", "持有/观望")
    analysis = ai_result.get("analysis", "")
    confidence = ai_result.get("confidence", 0)

    # 构建兼容的 decision 结构，供下游（app/webhook）消费
    fund_data["decision"] = {
        "action": advice,
        "details": [analysis],
    }

    # 4. 入库
    inds = fund_data.get("indicators", {})
    save_inspection_result(
        fund_code, fund_name,
        advice if not has_error else "数据异常",
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
        "fund_data": fund_data,
        "ai_report": ai_result,
        "has_error": has_error,
        "sync_error": None,
    }
