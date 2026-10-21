"""Read-only chart data, using the same sessions and formulae as inspections."""
import pandas as pd

from .engine import feature_frame
from .factors import BASIC_FIELDS, TECH_FIELDS
from .presentation import label


RANGES = {'近1月':pd.DateOffset(months=1),'近3月':pd.DateOffset(months=3),
          '近1年':pd.DateOffset(years=1),'近3年':pd.DateOffset(years=3),'全部':None}
METRICS = {
    'price':('价格与均线','点位','收盘点位与均线同图比较。均线按完整历史先计算，再截取显示范围。'),
    'returns':('阶段涨跌幅','%','分别相对5、20、60个交易日前收盘计算；不是账户收益。'),
    'pe':('市盈率','倍','仅展示正市盈率，零、负值与缺失保留为空。'),
    'pb':('市净率','倍','仅展示正市净率，零、负值与缺失保留为空。'),
    'valuation':('估值历史分位','%','每一天只与当时可用的历史比较，沿用本次规则的估值窗口；不是用今天的分位回填过去。'),
    'price_percentile':('点位历史分位','%','收盘点位在截至当日全部有效历史中的位置，至少20个有效样本；不是高估或低估的结论。'),
    'amount':('成交额','亿元','仅展示已确认人民币单位的成交额，并换算为亿元；未确认单位不混画。'),
    'amount_ratio':('成交活跃度','倍','当日成交额除以过去20个交易日平均成交额；1倍表示与均值相同。'),
    'volatility':('近期波动','%','过去20个交易日日收益的年化波动；不是预计涨跌幅，也不是最大亏损。'),
    'drawdown':('距阶段高点跌幅','%','当日收盘距过去120个交易日最高收盘的跌幅；不是期间最大回撤。'),
    'slope':('规则均线变化','%','规则均线相对指定观察天数前的变化，与巡检判断口径一致。'),
    'confirmation':('均线连续确认','交易日','连续高于或低于规则均线的天数；缺失交易日和相等点位会中断计数。'),
    'implicit_roe':('市净率÷市盈率','%','仅为PB÷PE推算比值，不是真实披露的净资产收益率，不据此推断盈利质量。'),
    'professional':('更多基础与专业指标','接口口径','只展示该份数据实际包含的指标，沿用接口数值；不同单位不混画。'),
}


def history_frame(manifest, spec):
    """Never query newer observations or let display ranges change warm-up."""
    end = manifest.get('as_of')
    sessions = [d for d in manifest.get('sessions',[]) if end and d<=end]
    allowed = set(sessions)
    rows = [r for r in manifest.get('data',[]) if r['date'] in allowed]
    frame = feature_frame(rows,sessions,spec)
    if not rows:
        return frame
    for key in ('pe','pb'):
        frame[key] = frame[key].where(frame[key]>0)
    frame['price_percentile'] = frame.close.expanding(min_periods=20).rank(method='max',pct=True)
    # Amount is normalized to CNY by adapters. Unknown legacy units stay absent.
    units = frame['amount_unit'] if 'amount_unit' in frame else pd.Series('',index=frame.index)
    frame['amount_cny'] = frame.amount.where(units.eq('CNY'))/100_000_000
    frame['amount_cny_ma20'] = frame['amount_cny'].rolling(20,min_periods=20).mean()
    return frame


def professional_fields(frame):
    return [k for k in dict.fromkeys([*BASIC_FIELDS,*TECH_FIELDS])
            if k not in ('pe','pb') and k in frame and frame[k].notna().any()]


def price_fields(spec):
    return ['close',*[f'ma{n}' for n in sorted({20,60,120,250,spec['ma']})]]


def field_label(field, spec):
    if field.startswith('ma') and field[2:].isdigit():
        n = int(field[2:])
        return f'{n}日'+('规则均线' if n==spec['ma'] else '均线')
    return {'close':'收盘点位','return5':'近5日涨跌','return20':'近20日涨跌','return60':'近60日涨跌',
            'pe_percentile':'市盈率历史分位','pb_percentile':'市净率历史分位','price_percentile':'点位历史分位',
            'amount_cny':'当日成交额','amount_cny_ma20':'20日平均成交额','amount_ratio':'成交活跃度',
            'volatility20':'20日年化波动','drawdown120':'距120日高点跌幅',
            f'ma{spec["ma"]}_slope':f'{spec["ma"]}日规则均线近{spec["slope_window"]}日变化',
            'above':'连续高于均线','below':'连续低于均线',
            'implicit_roe_pb_pe':'市净率÷市盈率'}.get(field,label(field))


def series_for(frame, spec, metric, selected=None, professional=None):
    fields = {'price':selected if selected is not None else ['close',f'ma{spec["ma"]}'],
              'returns':['return5','return20','return60'],'pe':['pe'],'pb':['pb'],
              'valuation':['pe_percentile','pb_percentile'],'price_percentile':['price_percentile'],
              'amount':['amount_cny','amount_cny_ma20'],'amount_ratio':['amount_ratio'],
              'volatility':['volatility20'],'drawdown':['drawdown120'],
              'slope':[f'ma{spec["ma"]}_slope'],'confirmation':['above','below'],
              'implicit_roe':['implicit_roe_pb_pe'],'professional':[professional] if professional else []}[metric]
    data = pd.DataFrame(index=frame.index)
    for field in fields:
        data[field_label(field,spec)] = frame[field] if field in frame else float('nan')
    if METRICS[metric][1]=='%':
        data *= 100
    return data.rename_axis('日期')


def display_range(data, period, as_of):
    offset = RANGES[period]
    if data.empty or offset is None:
        return data.copy()
    # Anchor to the inspection's last session, not today's wall clock.
    start = (pd.Timestamp(as_of)-offset).date().isoformat()
    return data.loc[start:].copy()


def chart_rows(data):
    """Explicit segments stop a chart joining points across missing sessions."""
    parts = []
    for name in data.columns:
        values = pd.to_numeric(data[name],errors='coerce').replace([float('inf'),-float('inf')],float('nan'))
        segment = values.isna().cumsum()
        part = pd.DataFrame({'日期':pd.to_datetime(data.index),'数值':values.to_numpy(),
                             '曲线':name,'分段':[name+':'+str(x) for x in segment]})
        parts.append(part.dropna(subset=['数值']))
    return pd.concat(parts,ignore_index=True) if parts else pd.DataFrame(columns=['日期','数值','曲线','分段'])


def chart_spec(unit):
    return {'mark':{'type':'line','point':{'size':15},'strokeWidth':2,'clip':True},
            'encoding':{'x':{'field':'日期','type':'temporal','title':'日期','axis':{'format':'%Y-%m-%d','labelOverlap':True}},
                        'y':{'field':'数值','type':'quantitative','title':unit,'scale':{'zero':False}},
                        'color':{'field':'曲线','type':'nominal','legend':{'title':None,'orient':'bottom'},
                                 'scale':{'range':['#0f766e','#d97706','#2563eb','#be123c','#7c3aed','#0891b2']}},
                        'detail':{'field':'分段','type':'nominal'},
                        'order':{'field':'日期','type':'temporal'},
                        'tooltip':[{'field':'日期','type':'temporal','title':'日期','format':'%Y-%m-%d'},
                                   {'field':'曲线','type':'nominal','title':'曲线'},
                                   {'field':'数值','type':'quantitative','title':unit,'format':',.2f'}]},
            'height':320}
