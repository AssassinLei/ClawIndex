"""Versioned, bounded factor rules; all inputs belong to the observed session."""
import math

import pandas as pd


# Independent of the legacy database module (which has import-time side effects).
TECH_FIELDS = (
    'asi_bfq asit_bfq atr_bfq bbi_bfq bias1_bfq bias2_bfq bias3_bfq '
    'boll_lower_bfq boll_mid_bfq boll_upper_bfq brar_ar_bfq brar_br_bfq cci_bfq cr_bfq '
    'dfma_dif_bfq dfma_difma_bfq dmi_adx_bfq dmi_adxr_bfq dmi_mdi_bfq dmi_pdi_bfq '
    'downdays updays dpo_bfq madpo_bfq ema_bfq_5 ema_bfq_10 ema_bfq_20 ema_bfq_30 '
    'ema_bfq_60 ema_bfq_90 ema_bfq_250 emv_bfq maemv_bfq expma_12_bfq expma_50_bfq '
    'kdj_bfq kdj_d_bfq kdj_k_bfq ktn_down_bfq ktn_mid_bfq ktn_upper_bfq lowdays topdays '
    'ma_bfq_5 ma_bfq_10 ma_bfq_20 ma_bfq_30 ma_bfq_60 ma_bfq_90 ma_bfq_250 '
    'macd_bfq macd_dea_bfq macd_dif_bfq mass_bfq ma_mass_bfq mfi_bfq mtm_bfq mtmma_bfq '
    'obv_bfq psy_bfq psyma_bfq roc_bfq maroc_bfq rsi_bfq_6 rsi_bfq_12 rsi_bfq_24 '
    'taq_down_bfq taq_mid_bfq taq_up_bfq trix_bfq trma_bfq vr_bfq wr_bfq wr1_bfq '
    'xsii_td1_bfq xsii_td2_bfq xsii_td3_bfq xsii_td4_bfq'
).split()
BASIC_FIELDS = 'vol pe pe_ttm pb total_mv float_mv total_share float_share free_share turnover_rate turnover_rate_f'.split()
PROFILES = {
    'price_risk_v1': {'label':'价格风险过滤', 'fields':['volatility20','drawdown120'],
                      'description':'年化波动不超过45%，近120日回撤不超过25%；仅过滤入场。'},
    'technical_v1': {'label':'趋势与动量确认', 'fields':['macd_dif_bfq','macd_dea_bfq','dmi_pdi_bfq','dmi_mdi_bfq','dmi_adx_bfq','rsi_bfq_12'],
                     'description':'MACD与动向同向，趋势强度至少20，12日相对强弱在45至75；仅过滤入场。'},
    'valuation_liquidity_v1': {'label':'估值与量能过滤', 'fields':['pe_percentile','pb_percentile','amount_ratio','return60'],
                               'description':'市盈率与市净率分位不高于85%，量能至少为20日均值的80%，60日动量为正；仅过滤入场。'},
    'volatility_channel_v1': {'label':'波动与通道过滤', 'fields':['atr_bfq','close','boll_mid_bfq','boll_upper_bfq','rsi_bfq_24'],
                             'description':'真实波幅/点位不超过4%，处于布林中轨至上轨之间，24日相对强弱不高于75；仅过滤入场。'},
}


def profile(spec):
    return spec.get('factor_profile') if spec.get('name') == 'trend_factor_v2' else None


def entry_filter(feature, spec):
    key = profile(spec)
    if not key:
        return True, True
    fields = PROFILES[key]['fields']
    ready = all(isinstance(feature.get(k),(int,float)) and not isinstance(feature[k],bool)
                and math.isfinite(feature[k]) for k in fields)
    if not ready:
        return False, False
    f = feature
    if key == 'price_risk_v1':
        accepted = f['volatility20'] <= .45 and f['drawdown120'] >= -.25
    elif key == 'technical_v1':
        accepted = f['macd_dif_bfq'] > f['macd_dea_bfq'] and f['dmi_pdi_bfq'] > f['dmi_mdi_bfq'] and f['dmi_adx_bfq'] >= 20 and 45 <= f['rsi_bfq_12'] <= 75
    elif key == 'valuation_liquidity_v1':
        accepted = f['pe_percentile'] <= .85 and f['pb_percentile'] <= .85 and f['amount_ratio'] >= .8 and f['return60'] > 0
    else:
        accepted = 0 <= f['atr_bfq']/f['close'] <= .04 and f['boll_mid_bfq'] <= f['close'] <= f['boll_upper_bfq'] and 0 <= f['rsi_bfq_24'] <= 75
    return bool(accepted), True


def coverage(frame, start=None):
    view = frame.loc[start:] if start else frame
    result = {}
    for key, details in PROFILES.items():
        present = pd.Series(True,index=view.index)
        for name in details['fields']:
            present &= view[name].notna() if name in view else False
        count = int(present.sum())
        result[key] = {**details,'sessions':len(view),'complete_sessions':count,
                       'coverage':count/len(view) if len(view) else 0.,'eligible':bool(len(view) and present.all())}
    return result


def authorized(spec, anchor, config):
    """Old authorizations remain MA-only; new profiles require explicit saved scope."""
    if not config['ma_min'] <= spec['ma'] <= config['ma_max']:
        return False
    immutable = ('confirmation','slope_window','valuation_window','annual_sessions')
    if any(spec[k] != anchor[k] for k in immutable):
        return False
    if spec['name'] == anchor['name'] and profile(spec) == profile(anchor):
        return True
    return config.get('factor_research',False) and (spec['name'] == 'trend_confirm_v1' or profile(spec) in config.get('factor_profiles',[]))


def candidates(base, config, frame, evaluation_start):
    mas = sorted({base['ma'],max(config['ma_min'],base['ma']-30),min(config['ma_max'],base['ma']+30)})
    variants = [{**base,'ma':n} for n in mas]
    availability = coverage(frame,evaluation_start)
    if config.get('factor_research',False):
        core = {k:v for k,v in base.items() if k != 'factor_profile'}
        for key in config.get('factor_profiles',[]):
            if availability[key]['eligible']:
                variants.extend({**core,'name':'trend_factor_v2','factor_profile':key,'ma':n} for n in mas)
        if base['name'] != 'trend_confirm_v1':
            variants.extend({**core,'name':'trend_confirm_v1','ma':n} for n in mas)
    unique = {tuple(sorted(v.items())):v for v in variants}
    return list(unique.values()),availability


def diagnostics(frame, evaluation_start):
    """Explore every collected factor in development only, never select a rule."""
    view = frame.loc[evaluation_start:]
    view = view.iloc[:int(len(view)*.6)]
    future_return = view.close.shift(-20)/view.close-1
    future_low = pd.concat([view.close.shift(-n)/view.close-1 for n in range(1,21)],axis=1).min(axis=1,skipna=False)
    rows = []
    for key in [*TECH_FIELDS,*BASIC_FIELDS,'amount_ratio','return60','volatility20','drawdown120','pe_percentile','pb_percentile']:
        if key not in view:
            continue
        paired = pd.DataFrame({'factor':view[key],'return':future_return,'low':future_low}).dropna()
        eligible = len(paired)>=63 and paired.factor.nunique()>1
        ranked = paired.rank()
        def correlation(target):
            value = ranked.factor.corr(ranked[target]) if eligible and ranked[target].nunique()>1 else None
            return float(value) if value is not None and math.isfinite(value) else None
        rows.append({'factor':key,'observed_sessions':int(view[key].notna().sum()),'paired_sessions':len(paired),
                     'return_rank_correlation':correlation('return'),'future_low_rank_correlation':correlation('low')})
    return {'interval':'DEVELOPMENT_ONLY','start':view.index[0] if len(view) else None,'end':view.index[-1] if len(view) else None,
            'horizon':20,'factors':rows,'used_for_selection':False,
            'limitations':['仅在开发区间探索，未来20日标签不会越过开发边界',
                          '窗口重叠、指标相关和非平稳会夸大表面关系；相关系数不是胜率，也不是因果或显著性证明',
                          '不按这张表临时选因子或调阈值，晋升只限固定授权组合']}
