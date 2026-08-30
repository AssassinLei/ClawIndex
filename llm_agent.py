import openai
import os
import json
import re
from pathlib import Path
from typing import Dict
from logger import setup_logger
from database import get_custom_prompt, get_selected_indicators

logger = setup_logger("llm_agent")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

_client = None

def _get_client():
    """延迟初始化 OpenAI client，避免 import 阶段因缺少 API_KEY 崩溃"""
    global _client
    if _client is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量，请检查 .env 文件")
        _client = openai.OpenAI(api_key=api_key, base_url=BASE_URL)
    return _client

# 可提供给 AI 的指标清单：key → (中文标签, 格式化函数)
INDICATOR_META: dict = {
    "price":          ("当前价格",            lambda v: f"{v:.3f}" if v is not None else None),
    "pe":             ("市盈率 (PE)",          lambda v: f"{v:.2f}" if v is not None else None),
    "pb":             ("市净率 (PB)",          lambda v: f"{v:.2f}" if v is not None else None),
    "pe_percentile":  ("PE 历史分位",          lambda v: f"{v*100:.2f}%" if v is not None else None),
    "pb_percentile":  ("PB 历史分位",          lambda v: f"{v*100:.2f}%" if v is not None else None),
    "roe":            ("净资产收益率 (ROE)",     lambda v: f"{v*100:.2f}%" if v is not None else None),
    "risk_premium":   ("风险溢价",             lambda v: f"{v*100:.2f}%" if v is not None else None),
    "ma60":           ("60日均线",             lambda v: f"{v:.3f}" if v is not None else None),
    "ma120":          ("120日均线",            lambda v: f"{v:.3f}" if v is not None else None),
    "amount":         ("成交额 (万元)",         lambda v: f"{v:.0f}" if v is not None else None),
    "amount_ma20":    ("20日均成交额",          lambda v: f"{v:.0f}" if v is not None else None),
    "price_percentile": ("价格历史分位",        lambda v: f"{v*100:.2f}%" if v is not None else None),
    "pct_chg":        ("当日涨跌幅",             lambda v: f"{v:.2f}%" if v is not None else None),
}

# 指标分组（用于 user_prompt 中按类别展示）
INDICATOR_GROUPS = [
    ("基本面数据", ["pe", "pe_percentile", "pb", "pb_percentile", "roe", "risk_premium"]),
    ("行情与技术面", ["price", "ma60", "ma120", "amount", "amount_ma20"]),
]

# 未配置指标筛选时的默认指标（与 UI 默认勾选一致）
DEFAULT_INDICATORS = ["pe", "pb"]

# 国际指数专属：指标分组与默认指标（无 PE/PB 估值，仅价格与趋势类指标）
GLOBAL_INDICATOR_GROUPS = [
    ("价格与趋势", ["price", "price_percentile", "ma60", "ma120"]),
    ("当日表现", ["pct_chg"]),
]
GLOBAL_DEFAULT_INDICATORS = ["price", "price_percentile", "ma60", "ma120", "pct_chg"]


def default_indicators_for(category: str) -> list[str]:
    """按策略分类返回未配置时的默认传递指标。

    scheduler 签名归一化与 app.py 提示词默认勾选共用本函数，
    保证「未配置用户」与「显式勾选默认指标的用户」归一化后合并为同一组（LLM 去重不破）。
    """
    if category == 'global':
        return GLOBAL_DEFAULT_INDICATORS
    return DEFAULT_INDICATORS


def indicator_groups_for(category: str) -> list:
    """按策略分类返回指标展示分组（提示词勾选 UI 与 user_prompt 生成共用）"""
    if category == 'global':
        return GLOBAL_INDICATOR_GROUPS
    return INDICATOR_GROUPS


def _build_indicators_text(inds: dict, selected: list[str] | None, groups: list | None = None) -> str:
    """按用户勾选的指标和分组，构建 user_prompt 中的指标展示文本"""
    keys = selected if selected else DEFAULT_INDICATORS
    groups = groups if groups is not None else INDICATOR_GROUPS
    lines = []
    for group_label, group_keys in groups:
        parts = []
        for key in group_keys:
            if key not in keys:
                continue
            val = inds.get(key)
            if val is None:
                continue
            label, fmt = INDICATOR_META[key]
            formatted = fmt(val)
            if formatted is not None:
                parts.append(f"{label} {formatted}")
        if parts:
            lines.append(f"- **{group_label}**：{'，'.join(parts)}")
    if not lines:
        lines.append("- **数据**：暂无可用指标")
    return "\n    ".join(lines)


# 合法的 AI 操作建议值
VALID_ADVICE = frozenset({"买入", "卖出", "持有/观望"})

# 预编译 JSON 代码块提取正则
_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*\n?([\s\S]*?)\n?```')

# 四类策略的自然语言描述（从旧硬编码规则树翻译而来，供 AI 参考）
CATEGORY_STRATEGIES = {
    "wide_base": (
        "宽基指数策略：侧重PE历史分位与无风险利率的对比，基于均值回归逻辑。"
        "PE分位低于20%为极度低估区间，高于80%为高估区间。"
        "结合60日均线判断趋势方向，价格站上MA60视为趋势确认信号。"
    ),
    "tech_growth": (
        "科技成长策略：侧重PE分位与120日牛熊线的关系，强调趋势确认以防左侧接飞刀。"
        "PE分位低于30%且站上MA120为右侧买入信号。"
        "跌破MA120应警惕趋势破位风险，暂停加仓保护本金。"
    ),
    "cycle_mfg": (
        "周期制造策略：侧重PB历史分位，周期行业以资产安全垫为核心判断依据。"
        "PB分位低于15%视为周期底部，高于85%视为景气度见顶信号。"
        "PB分位在15%~85%区间的中枢区域无明确买卖信号。"
    ),
    "dividend": (
        "稳健收息策略：侧重PB分位与PE绝对值，兼顾股息率与国债收益率利差。"
        "PB分位低于50%且PE低于15为合理估值区间，提供充分安全垫。"
        "价格跌破MA120时趋势向下，仅适合定投不宜单笔大额加仓。"
    ),
    "global": (
        "国际指数趋势跟踪策略：以双均线趋势为核心，不依赖估值指标。"
        "价格站上MA60且MA60高于MA120（多头排列）为买入/持有信号。"
        "价格跌破MA60或MA60下穿MA120为减仓/卖出信号。"
        "价格历史分位辅助判断当前价格在近10年区间的高低位置，分位越高追高风险越大。"
    ),
}

# prompt.md 文件路径
_PROMPT_MD_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt_md() -> str:
    """从 prompt.md 读取基础提示词内容"""
    if _PROMPT_MD_PATH.exists():
        return _PROMPT_MD_PATH.read_text(encoding="utf-8").strip()
    logger.warning(f"prompt.md 不存在: {_PROMPT_MD_PATH}，使用最小化默认提示词")
    return "你是一名专业的指数基金投资分析助手。基于第一性原理进行逻辑推演，仅依据输入数据分析。"


def _parse_ai_json(raw: str) -> dict:
    """三级容错提取 AI 输出的 JSON，返回 {analysis, advice, confidence}"""
    parsed = None

    # L1: 直接解析全文
    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # L2: 正则提取 markdown 代码块后解析
    if parsed is None:
        match = _JSON_BLOCK_RE.search(raw)
        if match:
            try:
                parsed = json.loads(match.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                pass

    # 校验字段
    if parsed and isinstance(parsed, dict):
        advice = str(parsed.get("advice", "")).strip()
        if advice not in VALID_ADVICE:
            advice = "持有/观望"
        analysis = str(parsed.get("analysis", raw))
        try:
            confidence = int(parsed.get("confidence", 50))
        except (ValueError, TypeError):
            confidence = 50
        confidence = max(0, min(100, confidence))  # 钳位 0~100
        return {"analysis": analysis, "advice": advice, "confidence": confidence}

    # L3: 兜底默认值（AI 有实质产出仅格式不合规，不属于 AI 失败，不加 ai_error 标记）
    logger.warning(f"_parse_ai_json: JSON 解析失败，使用兜底值。原始输出前200字符: {raw[:200]}")
    return {"analysis": raw, "advice": "持有/观望", "confidence": 0}


def _build_system_prompt(category: str, custom_prompt: str | None) -> str:
    """构建 System Prompt：prompt.md 基础 + 策略描述 + JSON Schema 约束"""

    json_schema = (
        "# 输出格式（严格遵守）\n"
        "你必须**仅**输出以下 JSON 结构，不要包含任何其他文字、解释或 Markdown 标记：\n"
        '{"analysis": "简洁清晰的分析说明，解释触发策略的关键原因", '
        '"advice": "买入|卖出|持有/观望", '
        '"confidence": 0-100}\n\n'
        "字段说明：\n"
        '- analysis: 基于第一性原理的推导分析，简洁易理解\n'
        '- advice: 唯一操作建议，必须是 "买入"、"卖出" 或 "持有/观望" 之一\n'
        '- confidence: 当前数据与策略的匹配程度（0~100），不是未来上涨概率\n\n'
        "直接输出 JSON，不要加 ```json 标记！"
    )

    if custom_prompt and custom_prompt.strip():
        # 用户定制提示词：用户已含 Role/Task，不叠加系统基础提示词，仅追加输出格式
        prompt = custom_prompt.strip()
    else:
        base = _load_prompt_md()
        strategy_desc = CATEGORY_STRATEGIES.get(category, "根据指数分类标签，参考通用估值分析逻辑。")
        prompt = f"{base}\n\n# 投资策略框架\n{strategy_desc}"

    prompt += f"\n\n{json_schema}"
    return prompt


def generate_ai_report(fund_data: Dict, custom_prompt: str = None, selected_indicators: list[str] | None = None) -> dict:
    """
    根据指标数据，由 AI 自主分析并输出结构化决策。

    参数 custom_prompt: 用户为指数定制的分析框架，注入 System Prompt 作为补充策略。
    参数 selected_indicators: 用户勾选的指标列表，None 表示全部。

    返回: {"analysis": str, "advice": str, "confidence": int}；
    AI 调用失败（API 异常/空响应）时额外含 "ai_error": True，下游据此跳过入库与推送。
    注意三类语义：数据异常（fund_data 含 error，仍入库）、AI 调用失败（ai_error，不入库）、
    JSON 解析兜底（AI 有产出仅格式不合规，照常入库）。
    """
    if "error" in fund_data:
        # 数据异常短路：属于"数据异常"语义（仍入库），不属于 AI 失败，不加 ai_error 标记
        logger.warning(f"generate_ai_report: 数据异常, 跳过AI调用 - {fund_data['error']}")
        return {
            "analysis": f"数据获取异常：{fund_data['error']}",
            "advice": "持有/观望",
            "confidence": 0,
        }

    code = fund_data['fund_code']
    name = fund_data.get('fund_name', code)
    cat = fund_data['category']
    inds = fund_data['indicators']

    # 构建 System Prompt
    system_prompt = _build_system_prompt(cat, custom_prompt)

    # 构建 User Prompt（动态按勾选指标与分类分组生成）
    effective_selected = selected_indicators or default_indicators_for(cat)
    indicators_text = _build_indicators_text(inds, effective_selected, groups=indicator_groups_for(cat))
    if selected_indicators:
        logger.info(f"generate_ai_report: {code} 已筛选指标 {selected_indicators}")

    user_prompt = f"""请根据以下指数数据进行分析：

- **监控指数**：{name}（{code}）(分类标签: {cat})
{indicators_text}

请输出你的 JSON 分析结果。"""

    try:
        logger.info(f"generate_ai_report: {code} (category={cat}), 调用 {MODEL_NAME}")

        # 记录发送给 AI 的完整内容（DEBUG 级别，生产环境可关闭控制台输出）
        logger.debug(
            f"generate_ai_report: {code} → AI 请求内容\n"
            f"--- System Prompt ---\n{system_prompt}\n"
            f"--- User Prompt ---\n{user_prompt}\n"
            f"--- 结束 ---"
        )

        response = _get_client().chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=16384
        )
        choice = response.choices[0] if response.choices else None
        raw = choice.message.content if choice else None
        finish_reason = choice.finish_reason if choice else "N/A（无 choices）"

        # 记录 token 用量
        usage = response.usage
        if usage:
            logger.debug(
                f"generate_ai_report: {code} token 用量 "
                f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
                f"total={usage.total_tokens}"
            )

        # 记录 AI 返回的原始内容（DEBUG 级别）
        raw_len = len(raw) if raw else 0
        logger.debug(
            f"generate_ai_report: {code} ← AI 返回内容 ({raw_len} 字符) "
            f"finish_reason={finish_reason}\n"
            f"--- 原始输出 ---\n{raw or '(空)'}\n"
            f"--- 结束 ---"
        )

        # 空响应：属于 AI 调用失败，标记 ai_error 供下游跳过入库/推送
        if not raw:
            logger.warning(
                f"generate_ai_report: {code} AI 返回空内容！"
                f" finish_reason={finish_reason}"
                f" (若为 content_filter 则说明 prompt 被安全过滤拦截)"
            )
            return {
                "analysis": f"AI 返回空内容（finish_reason={finish_reason}，可能被安全过滤拦截）",
                "advice": "持有/观望",
                "confidence": 0,
                "ai_error": True,
            }

        logger.info(f"generate_ai_report: {code} 返回 {raw_len} 字符, finish_reason={finish_reason}")
        return _parse_ai_json(raw)
    except openai.RateLimitError as e:
        # 注意：RateLimitError 是 APIError 子类，必须在前否则永远不可达
        logger.error(f"generate_ai_report: RateLimitError - {e}")
        return {"analysis": f"AI 接口限流: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}
    except openai.APIError as e:
        logger.error(f"generate_ai_report: APIError - {e}")
        return {"analysis": f"AI 接口调用失败 [{type(e).__name__}]: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}
    except Exception as e:
        logger.error(f"generate_ai_report: 未知异常 - {type(e).__name__}: {e}")
        return {"analysis": f"AI 报告生成失败 [{type(e).__name__}]: {str(e)}", "advice": "持有/观望", "confidence": 0, "ai_error": True}


def generate_ai_report_with_config(fund_data: Dict, username: str) -> dict:
    """
    高层封装：自动从数据库读取该用户对该指数的定制提示词与指标勾选配置，
    再调用 generate_ai_report。调用方无需关心数据库查询细节。
    """
    code = fund_data.get('fund_code', '')
    custom_prompt = get_custom_prompt(username, code)
    selected_indicators = get_selected_indicators(username, code) or None
    return generate_ai_report(
        fund_data,
        custom_prompt=custom_prompt,
        selected_indicators=selected_indicators,
    )