"""Lazy history charts; opening charts does not enqueue work or call providers."""
import json

import streamlit as st

from .engine import ALGORITHM
from .storage import canonical
from .trend_charts import (METRICS,RANGES,chart_rows,chart_spec,display_range,field_label,
                           history_frame,price_fields,professional_fields,series_for)


@st.cache_data(show_spinner=False,max_entries=32)
def cached_frame(manifest, spec):
    return history_frame(json.loads(manifest),json.loads(spec))


def trend_panel(svc, code, key, signal_id=None):
    try:
        source = svc.trend_source(code,signal_id)
    except (ValueError,PermissionError,RuntimeError) as exc:
        st.info(str(exc))
        return
    manifest,spec = source['manifest'],source['spec']
    st.markdown('#### 历史曲线')
    st.caption(('本次巡检保存的数据' if signal_id else '后台已同步的最新可用数据')+
               ' · '+source['name']+' · 行情截至 '+(manifest.get('as_of') or '尚未就绪')+
               f" · {spec['ma']}日规则均线")
    st.caption('来源：行情与指标数据、程序计算；不是AI生成。鼠标移到曲线上可查看日期和数值。')
    if source.get('algorithm') and source['algorithm']!=ALGORITHM:
        st.warning('此巡检使用较早的指标算法。下图按原始行情与原规则、使用当前算法计算，仅供查看走势，可能与当时的派生指标略有不同。')
    frame = cached_frame(canonical(manifest),canonical(spec))
    if frame.empty:
        st.info('后台尚未同步到可展示的历史行情，准备好后会显示；无需手动补数据。')
        return
    prefix = f'trend_{svc.user["id"]}_{key}_{code}_'
    cols = st.columns([1,2])
    metric = cols[0].selectbox('曲线指标',list(METRICS),format_func=lambda k:METRICS[k][0],key=prefix+'metric')
    period = cols[1].radio('显示范围',list(RANGES),index=2,horizontal=True,key=prefix+'range')
    selected,professional = None,None
    if metric=='price':
        selected = st.multiselect('叠加曲线',price_fields(spec),default=['close',f'ma{spec["ma"]}'],
                                  format_func=lambda k:field_label(k,spec),key=prefix+'overlays')
    elif metric=='professional':
        available = professional_fields(frame)
        if not available:
            st.info('这份数据尚未包含可展示的基础或专业指标；适用指标由后台自动维护，海外接口可能不提供这些数据。')
            return
        professional = st.selectbox('基础或专业指标',available,format_func=lambda k:field_label(k,spec),key=prefix+'professional')
    title,unit,description = METRICS[metric]
    st.caption(description)
    if metric=='valuation':
        st.caption(f"估值窗口：最近{spec['valuation_window']}个交易日，至少20个有效正值样本。")
    data = display_range(series_for(frame,spec,metric,selected,professional),period,manifest['as_of'])
    rows = chart_rows(data)
    if rows.empty:
        st.info('当前指标与显示范围内暂无有效数值；可切换指标或范围。缺失值不会当作零；适用数据由后台自动维护。')
        return
    st.vega_lite_chart(rows,chart_spec(unit),width='stretch',key=prefix+'chart')
    valid = data.notna().any(axis=1)
    st.caption(f"显示区间 {data.index[0]} 至 {data.index[-1]} · {len(data)}个交易日，其中{int(valid.sum())}日有有效数值。缺失处断开，不填平；纵轴按数据范围缩放。")
    st.caption('图表仅描述历史走势；指数点位涨跌不等于基金或账户收益，图中均线不代表实际成交。')
    with st.expander('查看曲线数据并导出',key=prefix+'table',on_change='rerun') as table:
        if table.open:
            st.dataframe(data.reset_index(),hide_index=True,width='stretch')
            st.download_button('下载当前曲线数据',data.to_csv().encode('utf-8-sig'),
                               file_name=code+'-'+metric+'-'+manifest['as_of']+'.csv',mime='text/csv',key=prefix+'csv')


def trend_expander(svc, code, key, signal_id=None):
    with st.expander('查看历史曲线',key=f'trend_open_{svc.user["id"]}_{key}_{code}',on_change='rerun') as panel:
        if panel.open:
            trend_panel(svc,code,key,signal_id)
