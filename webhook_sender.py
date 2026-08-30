"""
ClawIndex Webhook 消息推送模块
- 飞书自定义机器人 post 富文本消息
- 巡检完成后向配置的 webhook 地址发送结构化卡片
"""
import requests
import json
from typing import Dict, List
from datetime import datetime
from logger import setup_logger
from constants import CATEGORY_NAMES

logger = setup_logger("webhook_sender")

# 操作建议视觉映射
ACTION_LABELS = {
    "买入":      "🟢 建议买入",
    "卖出":      "🔴 建议卖出",
    "持有/观望":  "🟡 持有观望",
    "数据异常":   "⚠️ 数据异常",
}

# 仅这些操作建议允许推送 webhook 卡片（用户级开关 webhook_strong_signal_only 控制）
PUSH_ACTIONS = frozenset({"买入", "卖出"})


def is_action_pushable(fund_data: Dict) -> bool:
    """判断巡检结果是否属于应推送的强信号（decision.action ∈ PUSH_ACTIONS）"""
    return fund_data.get("decision", {}).get("action", "") in PUSH_ACTIONS


def _format_indicator(value, fmt: str = "{:.2f}", fallback: str = "N/A") -> str:
    """安全格式化指标值，None 时返回 fallback"""
    if value is None:
        return fallback
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def build_feishu_post(fund_data: Dict) -> tuple:
    """
    构建飞书 post 富文本消息内容。
    返回 (title, content) 元组，content 为飞书 post.content 数组。
    """
    code = fund_data.get("fund_code", "N/A")
    name = fund_data.get("fund_name", code)
    cat = fund_data.get("category", "")
    cat_name = CATEGORY_NAMES.get(cat, cat)
    inds = fund_data.get("indicators", {})
    decision = fund_data.get("decision", {})
    action = decision.get("action", "持有/观望")
    details = decision.get("details", [])
    has_error = "error" in fund_data

    action_label = ACTION_LABELS.get(action, ACTION_LABELS["持有/观望"])
    today = datetime.now().strftime("%Y-%m-%d")

    title = f"📊 巡检 | {code} {name} · {cat_name}"
    content = []

    # 段落 1：操作建议 + 日期
    content.append([
        {"tag": "text", "text": f"🎯 操作建议：{action_label}\n📅 巡检日期：{today}"},
    ])

    if has_error:
        error_msg = fund_data.get("error", "未知错误")
        content.append([{"tag": "text", "text": f"\n❌ 数据异常：{error_msg}"}])
        return title, content

    # 段落 2：核心指标（国际指数无估值数据，展示价格与趋势指标）
    if fund_data.get("category") == "global":
        price = _format_indicator(inds.get("price"), "{:.3f}")
        if inds.get("price_percentile") is not None:
            price_pct = f"{inds['price_percentile'] * 100:.1f}%"
        else:
            price_pct = "N/A"
        pct_chg = _format_indicator(inds.get("pct_chg"), "{:.2f}%")
        ma60 = _format_indicator(inds.get("ma60"), "{:.3f}")
        ma120 = _format_indicator(inds.get("ma120"), "{:.3f}")

        content.append([
            {"tag": "text", "text": (
                f"\n━━━ 📈 核心指标 ━━━\n"
                f"  当前价格：{price}\n"
                f"  价格历史分位：{price_pct}\n"
                f"  当日涨跌幅：{pct_chg}\n"
                f"  MA60：{ma60}     MA120：{ma120}"
            )},
        ])
    else:
        price = _format_indicator(inds.get("price"), "{:.3f}")
        pe = _format_indicator(inds.get("pe"))
        pb = _format_indicator(inds.get("pb"))
        if inds.get("pe_percentile") is not None:
            pe_pct = f"{inds['pe_percentile'] * 100:.1f}%"
        else:
            pe_pct = "N/A"
        rp = _format_indicator(inds.get("risk_premium"), "{:.4f}")
        if inds.get("roe") is not None:
            roe = f"{inds['roe'] * 100:.2f}%"
        else:
            roe = "N/A"

        content.append([
            {"tag": "text", "text": (
                f"\n━━━ 📈 核心指标 ━━━\n"
                f"  当前价格：{price}\n"
                f"  市盈率(PE)：{pe}     分位：{pe_pct}\n"
                f"  市净率(PB)：{pb}     ROE：{roe}\n"
                f"  风险溢价：{rp}"
            )},
        ])

    # 段落 3：AI 分析
    details_text = "\n".join(f"  • {d}" for d in details) if details else "  • 无（数据异常）"
    content.append([
        {"tag": "text", "text": f"\n━━━ 💡 AI 分析 ━━━\n{details_text}"},
    ])

    # 尾部
    content.append([
        {"tag": "text", "text": "\n---\nClawIndex 爪析 · 量化策略巡检"},
    ])

    return title, content


def send_inspection_card(webhook_url: str, fund_data: Dict, label: str = "") -> bool:
    """
    向指定 webhook 地址发送巡检卡片消息。
    返回 True 表示发送成功，False 表示失败。
    失败时仅记录日志，不抛出异常。
    """
    code = fund_data.get("fund_code", "?")
    title, post_content = build_feishu_post(fund_data)

    # 飞书自定义机器人 post 富文本格式
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": post_content,
                },
            },
        },
    }

    # 发送前日志：记录目标、标的
    url_preview = webhook_url[:80]
    label_info = f" [{label}]" if label else ""
    para_count = len(post_content)
    logger.info(
        f"send_inspection_card: 开始发送{label_info} "
        f"fund={code} title=\"{title}\" paragraphs={para_count} url={url_preview}..."
    )
    # 完整消息体写入日志，方便排查格式问题
    logger.info(
        f"send_inspection_card: 消息体{label_info} fund={code}\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        # 记录完整响应体（不截断），尝试解析 JSON 错误码
        raw_body = resp.text
        try:
            resp_json = json.loads(raw_body)
            errcode = resp_json.get("errcode", resp_json.get("code", "N/A"))
            errmsg = resp_json.get("errmsg", resp_json.get("msg", ""))
            body_summary = f"errcode={errcode} errmsg={errmsg}"
        except (json.JSONDecodeError, ValueError):
            body_summary = raw_body[:300]

        if resp.status_code == 200:
            logger.info(
                f"send_inspection_card: HTTP 200{label_info} "
                f"fund={code} {body_summary} url={url_preview}..."
            )
            return True
        else:
            logger.warning(
                f"send_inspection_card: HTTP {resp.status_code}{label_info} "
                f"fund={code} {body_summary} url={url_preview}..."
            )
            return False
    except requests.exceptions.Timeout:
        logger.warning(
            f"send_inspection_card: 请求超时{label_info} "
            f"fund={code} url={url_preview}..."
        )
        return False
    except requests.exceptions.ConnectionError as e:
        logger.warning(
            f"send_inspection_card: 连接失败{label_info} "
            f"fund={code} error={e} url={url_preview}..."
        )
        return False
    except Exception as e:
        logger.warning(
            f"send_inspection_card: 未知异常{label_info} "
            f"fund={code} {type(e).__name__}: {e} url={url_preview}..."
        )
        return False


def send_to_all_webhooks(
    webhook_urls: List[Dict],
    fund_data: Dict,
) -> Dict[str, int]:
    """
    向所有配置的 webhook 地址发送巡检卡片。
    返回 {"success": N, "fail": M} 统计结果。
    """
    success_count = 0
    fail_count = 0

    for wh in webhook_urls:
        url = wh.get("url", "")
        wh_label = wh.get("label", "")
        if not url:
            continue
        if send_inspection_card(url, fund_data, label=wh_label):
            success_count += 1
        else:
            fail_count += 1

    logger.info(
        f"send_to_all_webhooks: {success_count} 成功, {fail_count} 失败, "
        f"共 {len(webhook_urls)} 条地址"
    )
    return {"success": success_count, "fail": fail_count}
