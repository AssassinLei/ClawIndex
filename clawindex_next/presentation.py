"""Chinese display vocabulary; stored codes and investment rules remain unchanged."""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

LABELS = {
    'radar':'机会雷达','radar_notification':'机会提醒','relative60':'60日相对沪深300表现','relative120':'120日相对沪深300表现','return120':'近120日表现','extension20':'高于20日均线幅度','ma20':'20日均线','ma60_slope':'60日均线变化','above':'连续高于规则均线天数','pe_samples':'市盈率有效样本','pb_samples':'市净率有效样本',
    'WAITING_DATA':'等待后台准备行情','READY_TO_APPLY':'等待应用新规则','catalog':'指数搜索目录',
    "SUBMITTED":"已下单，等待成交确认", "FROZEN":"历史研究完成", "FORWARD_SHADOW":"新行情模拟观察",
    "ELIGIBLE":"观察通过，等待授权或更新条件", "ACTIVE":"已更新正式策略", "REJECTED":"未通过，保留现行策略",
    "QUARANTINED":"已隔离，保留现行策略", "PROMOTED":"规则已自动更新", "WAITING":"等待必要条件", "evolution":"自动进化", "RETIRED":"已回退停用",
    "BUY":"买入信号", "SELL":"卖出信号", "HOLD":"继续持有", "WAIT":"空仓等待",
    "UNKNOWN":"未知", "EMPTY":"空仓", "HELD":"持有", "LONG":"持有", "FLAT":"空仓",
    "RESEARCH":"研究观察", "SHADOW":"模拟跟踪", "ENABLED":"已启用提醒",
    "AVAILABLE":"判断已生成", "CONTEXT_REQUIRED":"请确认持仓或资金", "BLOCKED":"暂不能判断",
    "OK":"正常", "STALE":"行情过期", "INSUFFICIENT_DATA":"历史数据不足",
    "IDENTITY_UNVERIFIED":"指数身份待核实", "NO_DATA":"暂无行情", "NONE":"无新触发",
    "PENDING":"排队中", "RUNNING":"处理中", "SUCCEEDED":"已完成", "RETRY":"等待重试",
    "FAILED":"失败", "SENT":"已发送", "SENDING":"发送中", "EXPIRED":"已过期",
    "ACKNOWLEDGED":"已查看", "EXECUTED":"已执行", "PARTIAL":"部分执行",
    "DEFERRED":"暂缓执行", "CANCELLED":"已取消",
    "UP":"上行特征", "DOWN":"下行特征", "RANGE":"震荡特征", "UNAVAILABLE":"数据不足",
    "CNY":"人民币", "HKD":"港币", "USD":"美元", "CN":"中国内地", "HK":"中国香港", "US":"美国",
    "EQUITY_INDEX":"股票指数", "EQUITY_SECTOR_INDEX":"股票行业指数", "UNCERTAIN":"数据不足，特征不明确", "MODEL_OK":"AI生成的解读", "TEMPLATE":"程序生成的解读",
    "FAILED_TEMPLATE":"程序生成的解读（AI未成功）",
    "inspection":"巡检", "sync":"行情同步", "worker":"后台服务", "notification":"消息通知", "explanation":"分析解读",
    "queued":"排队中", "syncing":"正在更新行情", "analyzing":"正在计算信号",
    "explaining":"正在生成AI解读", "done":"结果已生成", "failed":"处理失败",
    "close":"收盘点位", "ma60":"60日均线", "ma120":"120日均线", "ma120_slope":"120日均线变化",
    "above":"连续高于均线天数", "below":"连续低于均线天数", "pe_percentile":"市盈率历史分位",
    "pe":"市盈率","pb":"市净率","pb_percentile":"市净率历史分位","amount_ratio":"成交额相对20日均值","volatility20":"20日年化波动","drawdown120":"近120日回撤",
    "pe_ttm":"滚动市盈率","vol":"成交量（接口口径）","total_mv":"总市值（接口口径）","float_mv":"流通市值（接口口径）","total_share":"总股本（接口口径）","float_share":"流通股本（接口口径）","free_share":"自由流通股本（接口口径）","turnover_rate":"换手率","turnover_rate_f":"自由流通股本换手率","return60":"60日动量",
    "NO_PERMISSION":"接口权限暂不可用","DELAYED":"等待接口数据","CHECKED":"本轮检查完成","SYNCING":"后台补充中",
    "pe_samples":"估值有效样本数", "regime":"市场特征", "strategy_ready":"分析数据是否齐全",
}


def label(value):
    from .factors import TECH_FIELDS
    if value in TECH_FIELDS:
        key = value.replace('_bfq','')
        names = {'asi':'振动升降','asit':'振动升降均值','atr':'平均真实波幅','bbi':'多空均线','bias1':'6日乖离率','bias2':'12日乖离率','bias3':'24日乖离率',
                 'boll_lower':'布林下轨','boll_mid':'布林中轨','boll_upper':'布林上轨','brar_ar':'人气指标','brar_br':'意愿指标','cci':'顺势指标','cr':'价格动量',
                 'dfma_dif':'平行线差','dfma_difma':'平行线差均值','dmi_adx':'动向趋势强度','dmi_adxr':'动向强度均值','dmi_mdi':'负向动向','dmi_pdi':'正向动向',
                 'downdays':'连续下跌天数','updays':'连续上涨天数','dpo':'区间震荡','madpo':'区间震荡均值','emv':'简易波动','maemv':'简易波动均值',
                 'kdj':'随机指标J值','kdj_d':'随机指标D值','kdj_k':'随机指标K值','ktn_down':'肯特纳下轨','ktn_mid':'肯特纳中轨','ktn_upper':'肯特纳上轨',
                 'lowdays':'近期最低范围天数','topdays':'近期最高范围天数','macd':'趋势动量柱','macd_dea':'趋势动量信号线','macd_dif':'趋势动量快慢线差',
                 'mass':'梅斯线','ma_mass':'梅斯线均值','mfi':'资金流量强弱','mtm':'价格动量','mtmma':'价格动量均值','obv':'能量潮','psy':'心理指标','psyma':'心理指标均值',
                 'roc':'变动率','maroc':'变动率均值','taq_down':'唐安奇下轨','taq_mid':'唐安奇中轨','taq_up':'唐安奇上轨','trix':'三重指数平滑','trma':'三重平滑均值',
                 'vr':'容量比率','wr':'威廉指标','wr1':'短期威廉指标'}
        if key in names: return names[key]
        if key.startswith('ema_'): return key[4:]+'日指数均线'
        if key.startswith('ma_'): return key[3:]+'日简单均线'
        if key.startswith('rsi_'): return key[4:]+'日相对强弱'
        if key.startswith('expma_'): return key[6:]+'日指数平均'
        if key.startswith('xsii_td'): return '薛斯通道线'+key[7:]
    return LABELS.get(value, str(value) if value is not None else "暂无")


def readable(text):
    text = re.sub(r"MA(\d+)", r"\1日均线", str(text))
    text = re.sub(r"均线斜率(-?\d+\.\d+)", lambda m:"均线变化"+f"{float(m.group(1)):.2%}",text)
    for code in ("CONTEXT_REQUIRED", "INSUFFICIENT_DATA", "RESEARCH", "SHADOW", "ENABLED", "BLOCKED", "AVAILABLE", "STALE", "BUY", "SELL", "HOLD", "WAIT", "UP", "DOWN", "RANGE"):
        text = re.sub(r"\b" + code + r"\b", label(code), text)
    return text


def number(value, percent=False):
    if value is None:
        return "暂无数据"
    try:
        v = float(value)
        if not math.isfinite(v):
            return "暂无数据"
        return f"{v:.2%}" if percent else f"{v:,.2f}"
    except (TypeError, ValueError):
        return "暂无数据"


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value or "暂无"
