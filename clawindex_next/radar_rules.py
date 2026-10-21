"""Frozen, exploratory cross-sectional rules. Rankings are never probabilities."""
import math

import numpy as np
import pandas as pd

from .engine import DEFAULT_SPEC, feature_frame, features_at
from .storage import digest


RULE = {
    'version':'opportunity_radar_v2','minimum_sessions':260,'relative_windows':[60,120],
    'volatility_limit':.45,'drawdown_limit':.20,'extension_limit':.10,
    'entry_confirmation':3,'valuation_minimum_samples':252,'valuation_minimum_distinct':20,'valuation_maximum_percentile':.30,
    'overvalued_percentile':.90,'correlation_sessions':120,'correlation_limit':.85,
    'minimum_coverage':.80,'minimum_breadth':.40,'maximum_candidates':3,
    'breadth_basis':'L1','volatility_windows':[20,60],'drawdown_window':120,'valuation_window':1250,
}
RULE_ID = digest(RULE)
STATES = {'READY':'研究入场条件满足','WATCH':'继续观察','REJECTED':'未达到条件','DATA_WAIT':'后台准备数据'}
KINDS = {'TREND':'趋势改善型','VALUE':'低估修复观察'}


def frame_for(rows, sessions):
    frame = feature_frame(rows,sessions,DEFAULT_SPEC)
    if not frame.empty:
        frame['return120'] = frame.close/frame.close.shift(120)-1
        frame['extension20'] = frame.close/frame.ma20-1
        frame['volatility60'] = np.log(frame.close/frame.close.shift()).rolling(60,min_periods=60).std(ddof=1)*math.sqrt(DEFAULT_SPEC['annual_sessions'])
        frame['max_drawdown120'] = frame.close.rolling(120,min_periods=120).apply(lambda values:float(np.min(values/np.maximum.accumulate(values)-1)),raw=True)
    return frame


def usable(frame):
    return bool(len(frame)>=RULE['minimum_sessions'] and frame.valid_price.iloc[-RULE['minimum_sessions']:].all())


def market_state(frame):
    if not usable(frame):
        return {'status':'UNKNOWN','entry_allowed':False,'reason':'比较基准由后台自动准备中'}
    f = features_at(frame,frame.index[-1])
    strong = f['close']>f['ma120'] and f['ma120_slope']>0 and f['return60']>0
    return {'status':'UP' if strong else 'CAUTIOUS','entry_allowed':bool(strong),
            'reason':'基准趋势向上，仍需逐个检查风险' if strong else '基准趋势尚未转强，候选最多进入观察，不升级入场条件',
            'return60':f['return60'],'close':f['close'],'ma120':f['ma120']}


def classify(item, frame, benchmark, market):
    result = {'code':item['code'],'name':item['name'],'level':item['metadata'].get('level'),
              'parent':item['metadata'].get('parent_code'),'industry_code':item['metadata'].get('industry_code'),
              'state':'REJECTED','kind':None,'reasons':[],'risks':[],'invalidations':[], 'features':{}}
    if not usable(frame) or not usable(benchmark) or frame.index[-1]!=benchmark.index[-1]:
        result.update(state='DATA_WAIT',reasons=['价格历史、完整交易时段或比较基准正在自动准备'])
        return result
    f = features_at(frame,frame.index[-1])
    b = features_at(benchmark,benchmark.index[-1])
    f.update(relative60=(1+f['return60'])/(1+b['return60'])-1,
             relative120=(1+f['return120'])/(1+b['return120'])-1)
    result['features'] = {k:f.get(k) for k in ('close','ma20','ma60','ma120','ma120_slope','above','return20','return60','return120','relative60','relative120','volatility20','volatility60','drawdown120','max_drawdown120','extension20','pe','pb','pe_percentile','pb_percentile','pe_samples','pb_samples','amount_ratio')}
    if any(f.get('volatility'+str(n)) is None or f['volatility'+str(n)]>RULE['volatility_limit'] for n in RULE['volatility_windows']):
        result['reasons'].append('近20日或60日年化波动超过45%的风险门槛，或波动数据未齐备')
    if f['max_drawdown120'] < -RULE['drawdown_limit']:
        result['reasons'].append('近120日期间最大收盘回撤超过20%的风险门槛')
    if f['extension20']>RULE['extension_limit']:
        result['reasons'].append('高于20日均线超过10%，暂不追涨')
    valuation_ready = all(f.get(k+'_samples',0)>=RULE['valuation_minimum_samples'] and f.get(k+'_percentile') is not None
                          and frame[k].iloc[-RULE['valuation_window']:].where(lambda values:values>0).nunique()>=RULE['valuation_minimum_distinct'] for k in ('pe','pb'))
    if valuation_ready and max(f['pe_percentile'],f['pb_percentile'])>RULE['overvalued_percentile']:
        result['reasons'].append('估值处于自身可用历史的高位，暂不新增候选')
    if result['reasons']:
        return result
    # Absolute positive performance is mandatory: being the least bad is not entry evidence.
    improving = f['return60']>0 and f['relative60']>0 and f['close']>f['ma60'] and f['ma60_slope']>0
    confirmed = improving and f['return120']>0 and f['relative120']>0 and f['above']>=RULE['entry_confirmation'] and f['ma120_slope']>0
    cheap = valuation_ready and f['pe_percentile']<=RULE['valuation_maximum_percentile'] and f['pb_percentile']<=RULE['valuation_maximum_percentile']
    stabilizing = f['return20']>0 and f['close']>f['ma20'] and f['ma20_slope']>0
    if improving:
        result.update(kind='TREND',state='READY' if confirmed and market['entry_allowed'] else 'WATCH')
        result['reasons'] = ['近60日自身上涨，且相对沪深300改善','点位高于60日均线，均线方向向上']
        if confirmed:
            result['reasons'].append('近120日相对表现与连续站上120日均线条件均已满足')
        else:
            result['risks'].append('中期相对表现或连续确认尚未齐备，继续观察')
    elif cheap and stabilizing:
        result.update(kind='VALUE',state='WATCH',reasons=['PE与PB均处于自身可用历史的较低区间','近20日出现价格企稳线索'])
        result['risks'].append('低估值可能来自盈利恶化，当前没有成分股盈利质量验证')
    else:
        result['reasons'] = ['尚未同时出现可核实的趋势改善或低估企稳条件']
        return result
    if not market['entry_allowed']:
        result['risks'].append(market['reason'])
    if not valuation_ready:
        result['risks'].append('估值样本长度或有效变化不足，未将其作为低估或安全边际依据')
    result['risks'].extend(['仅为固定规则的研究候选，尚无长期前向验证','指数表现不等于实际基金收益，跟踪基金及交易条件需另行核实'])
    result['invalidations'] = ['趋势失效、风险门槛不再满足或数据过期时撤销候选','观察状态不产生买入指令；加入监控后由个人规则继续判断']
    return result


def correlation(a, b):
    aligned = pd.concat([a.close.pct_change(fill_method=None),b.close.pct_change(fill_method=None)],axis=1).iloc[-RULE['correlation_sessions']:]
    if len(aligned)!=RULE['correlation_sessions'] or not aligned.notna().all().all():
        return None
    value = aligned.iloc[:,0].corr(aligned.iloc[:,1])
    return float(value) if math.isfinite(value) else None


def pair_key(a,b):
    return '|'.join(sorted((a,b)))


def rank(rows):
    return sorted((r for r in rows if r['state'] in ('READY','WATCH')),
                  key=lambda r:(r['state']!='READY',r['kind']!='TREND',-r['features']['relative60'],r['features']['volatility20'],r['code']))


def family(item):
    return item['code'] if item.get('level')=='L1' else (item.get('parent_index') or {}).get('code') or item.get('parent')


def select(rows, correlations, excluded=()):
    chosen,suppressed = [],[]
    for row in rank(rows):
        if row['code'] in excluded:
            continue
        conflict = None
        for existing in chosen:
            same_family = (row.get('parent') in (existing['code'],existing.get('industry_code')) if row.get('parent') else False) or (existing.get('parent') in (row['code'],row.get('industry_code')) if existing.get('parent') else False)
            same_family = same_family or bool(family(row) and family(row)==family(existing))
            corr = correlations.get(pair_key(row['code'],existing['code']))
            if same_family or corr is None or corr>=RULE['correlation_limit']:
                conflict = '与已展示候选属于同一行业链或走势高度同向' if same_family or corr is not None else '共同历史不足，无法可靠区分风险'
                break
        if conflict:
            suppressed.append({'code':row['code'],'name':row['name'],'reason':conflict})
        elif len(chosen)<RULE['maximum_candidates']:
            chosen.append(row)
    return chosen,suppressed


def analyze(items, frames, benchmark, sources=None):
    market = market_state(benchmark)
    rows = [classify(item,frames.get(item['code'],pd.DataFrame()),benchmark,market) for item in items]
    available = sum(row['state']!='DATA_WAIT' for row in rows)
    coverage = available/len(rows) if rows else 0.
    status = 'COMPLETE' if coverage>=RULE['minimum_coverage'] and market['status']!='UNKNOWN' else 'PREPARING'
    if status=='COMPLETE':
        sectors = [r for r in rows if r['level']=='L1']
        usable_sectors = [r for r in sectors if r['state']!='DATA_WAIT']
        breadth = sum(r['features'].get('return60',-1)>0 for r in usable_sectors)/max(1,len(usable_sectors))
        sector_coverage = len(usable_sectors)/len(sectors) if sectors else 0.
        market.update(breadth=breadth,breadth_basis='L1',breadth_available=len(usable_sectors),breadth_total=len(sectors),breadth_coverage=sector_coverage)
        if sector_coverage<RULE['minimum_coverage'] or breadth<RULE['minimum_breadth']:
            market.update(entry_allowed=False,reason='一级行业共同历史覆盖不足，候选保留观察' if sector_coverage<RULE['minimum_coverage'] else '多数已覆盖一级行业的中期表现仍弱，保留观察')
            rows = [classify(item,frames.get(item['code'],pd.DataFrame()),benchmark,market) for item in items]
    from .industry_hierarchy import enrich_rows
    rows = enrich_rows(rows, items)
    correlations = {}
    eligible = [r for r in rows if r['state'] in ('READY','WATCH')]
    for i,a in enumerate(eligible):
        for b in eligible[i+1:]:
            correlations[pair_key(a['code'],b['code'])] = correlation(frames[a['code']],frames[b['code']])
    selected,suppressed = select(rows,correlations) if status=='COMPLETE' else ([],[])
    return {'rule_id':RULE_ID,'rule':RULE,'status':status,'market':market,'coverage':coverage,'available':available,
            'total':len(items),'rows':rows,'selected':[r['code'] for r in selected],'suppressed':suppressed,
            'correlations':correlations,'sources':sources or {},'basis':'INDEX_PRICE_EXPLORATORY',
            'limitations':['固定研究规则，阈值未证明最优；本次口径修正不代表已经验证收益更好','历史分类和指数回溯可能产生偏差，发现排名不是赚钱概率','估值分位只与自身可用历史比较，不代表内在价值；不从技术指标推断行业盈利改善','同一级行业仅展示一个方向，降低重复暴露，也可能错过同一行业的其他机会']}
