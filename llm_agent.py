import openai
import os
from typing import Dict
from logger import setup_logger

logger = setup_logger("llm_agent")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

if not API_KEY:
    raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量，请检查 .env 文件")

client = openai.OpenAI(api_key=API_KEY, base_url=BASE_URL)

def generate_ai_report(fund_data: Dict) -> str:
    """
    根据硬编码输出的数据字典，通过大模型生成一段“大白话”投资建议。
    注意：Prompt 中严格限制了 AI 不能改变决策（Action）。
    """
    if "error" in fund_data:
        logger.warning(f"generate_ai_report: 数据异常, 跳过AI调用 - {fund_data['error']}")
        return f"❌ 数据获取异常：{fund_data['error']}"
        
    code = fund_data['fund_code']
    cat = fund_data['category']
    inds = fund_data['indicators']
    decision = fund_data['decision']
    
    # 格式化指标用于提示词
    pe_str = f"{inds['pe']:.2f}" if inds['pe'] is not None else "无数据"
    pe_pct_str = f"{inds['pe_percentile']*100:.2f}%" if inds['pe_percentile'] is not None else "无数据"
    roe_str = f"{inds['roe']*100:.2f}%" if inds['roe'] is not None else "无数据"
    
    # 构建 System Prompt
    system_prompt = """
你是一名严谨的量化基金策略执行助理，核心职责是把系统的量化判定结果翻译成清晰易懂的中文简报。

#【绝对红线（不可违反）】
1.  所有解释必须严格基于系统提供的量化数据和硬逻辑判定理由，禁止自行新增任何未提及的基本面、消息面、政策面因素。
2.  语气冷静、客观、中立，不使用煽动性表述，不做行情预测。
3.输出内容不使用Emoji图案

#【强制思维链（必须按此步骤输出）】
第一步：先明确复述系统的最终操作结论，放在最开头。
第二步：根据标的的分类标签，匹配对应类别的定价逻辑，解读估值面数据：
    - 宽基指数：侧重PE历史分位、盈利收益率与无风险利率的对比，均值回归逻辑
    - 科技成长：侧重PEG/PS分位、成长赛道的趋势属性，强调止损纪律
    - 周期制造：侧重PB历史分位、资产安全垫逻辑，说明周期行业估值的特殊性
    - 稳健收息：侧重股息率与国债收益率的利差，强调类债券属性和长期持有逻辑
第三步：解读技术面数据，说明均线状态与系统动作的对应关系（突破/跌破/震荡）。
第四步：给出1条明确的执行提示，以及1条适配该类别的风险提醒。
    """
    
    # 构建 User Prompt
    user_prompt = f"""
    请根据以下量化系统的输出数据，生成简报：
    
    - **监控指数**：{code} (分类标签: {cat})
    - **核心基本面数据**：当前 PE {pe_str}，处于历史 {pe_pct_str} 分位。推导 ROE 约为 {roe_str}。
    - **核心技术面数据**：当前价格 {inds['price']}，60日均线 {inds['ma60']}，120日均线 {inds['ma120']}。
    - **系统最终决策动作**：【{decision['action']}】
    - **系统硬逻辑判定理由**：{ '；'.join(decision['details']) }
    
    请输出你的分析简报。
    """

    try:
        logger.info(f"generate_ai_report: {code} action={decision['action']}, 调用 {MODEL_NAME}")
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3, # 极低温度，防止 AI 产生幻觉
            max_tokens=800
        )
        result = response.choices[0].message.content
        logger.info(f"generate_ai_report: {code} 返回 {len(result)} 字符")
        return result
    except openai.APIError as e:
        logger.error(f"generate_ai_report: APIError - {e}")
        return f"❌ AI 接口调用失败 [{type(e).__name__}]: {str(e)}\n\n请检查：1) DEEPSEEK_API_KEY 是否正确 2) API 服务是否可用 3) 网络连接是否正常"
    except openai.RateLimitError as e:
        logger.error(f"generate_ai_report: RateLimitError - {e}")
        return f"❌ AI 接口限流: {str(e)}\n\n请稍后重试或检查 API 配额"
    except Exception as e:
        logger.error(f"generate_ai_report: 未知异常 - {type(e).__name__}: {e}")
        return f"❌ AI 报告生成失败 [{type(e).__name__}]: {str(e)}"