import openai
import os
from typing import Dict

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
        return f"❌ 数据获取异常：{fund_data['error']}"
        
    code = fund_data['fund_code']
    cat = fund_data['category']
    inds = fund_data['indicators']
    decision = fund_data['decision']
    
    # 格式化指标用于提示词
    pe_str = f"{inds['pe']:.2f}" if inds['pe'] else "无数据"
    pe_pct_str = f"{inds['pe_percentile']*100:.2f}%" if inds['pe_percentile'] else "无数据"
    roe_str = f"{inds['roe']*100:.2f}%" if inds['roe'] else "无数据"
    
    # 构建 System Prompt
    system_prompt = """
    你是一个极其专业的量化基金经理。你的任务是将量化系统的冰冷判定结果，翻译成便于用户阅读的【中文投资简报】。
    
    【核心原则】：
    1. 你没有决策权，绝对不能改变系统输出的 `Action`（买卖建议）。你的作用只是“解释为什么”。
    2. 语气要冷静、客观，不要煽动情绪。
    3. 输出要精简，直接切中要害，分点阐述（当前估值情况、趋势情况、策略解释）。
    4. 不要暴露 json 数据结构，用大白话书写。
    """
    
    # 构建 User Prompt
    user_prompt = f"""
    请根据以下量化系统的输出数据，生成简报：
    
    - **监控标的**：{code} (分类标签: {cat})
    - **核心基本面数据**：当前 PE {pe_str}，处于历史 {pe_pct_str} 分位。推导 ROE 约为 {roe_str}。
    - **核心技术面数据**：当前价格 {inds['price']}，60日均线 {inds['ma60']}，120日均线 {inds['ma120']}。
    - **系统最终决策动作**：【{decision['action']}】
    - **系统硬逻辑判定理由**：{ '；'.join(decision['details']) }
    
    请输出你的分析简报。
    """

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3, # 极低温度，防止 AI 产生幻觉
            max_tokens=800
        )
        return response.choices[0].message.content
    except openai.APIError as e:
        return f"❌ AI 接口调用失败 [{type(e).__name__}]: {str(e)}\n\n请检查：1) DEEPSEEK_API_KEY 是否正确 2) API 服务是否可用 3) 网络连接是否正常"
    except openai.RateLimitError as e:
        return f"❌ AI 接口限流: {str(e)}\n\n请稍后重试或检查 API 配额"
    except Exception as e:
        return f"❌ AI 报告生成失败 [{type(e).__name__}]: {str(e)}"