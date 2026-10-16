"""One explicit, saved delivery schedule per Webhook; previews never send messages."""
from datetime import datetime

import streamlit as st

from .webhook_schedule import DEFAULT_CRON


def schedule_editor(svc, channel, guarded):
    cid = channel['id']
    config = svc.channel_schedule(cid)
    summary = st.empty()
    with st.expander('自定义发送时间（Cron）'):
        st.caption('只控制此Webhook的正式操作通知与机会通知；不影响分析和数据更新。“你好，世界”测试始终立即发送。')
        with st.form('webhook_schedule_'+str(cid)):
            enabled = st.checkbox('按Cron定时发送',value=config['enabled'],key='schedule_on_'+str(cid),help='取消勾选并保存会恢复即时发送。若要停止通知，请关闭Webhook总开关。')
            expression = st.text_input('Cron表达式',value=config['cron'] or DEFAULT_CRON,key='schedule_cron_'+str(cid),help='标准五字段：分 时 日 月 周。周字段0或7为周日，1为周一；建议用mon-fri等英文星期，避免歧义。')
            if st.form_submit_button('保存发送时间'):
                if guarded(lambda:svc.configure_channel_schedule(cid,enabled,expression)):
                    config = svc.channel_schedule(cid)
                    st.success('发送时间已保存，仅影响这个Webhook。')
        if config.get('error'):
            st.warning(config['error'])
        elif config['enabled']:
            st.write('已保存：`'+config['cron']+'` · 北京时间')
            st.caption('接下来三次计划时间：')
            for value in config['next_times']:
                instant = datetime.fromisoformat(value)
                st.caption(instant.strftime('%Y-%m-%d %H:%M')+' · '+['周一','周二','周三','周四','周五','周六','周日'][instant.weekday()])
        else:
            st.caption('当前为即时发送。勾选定时发送并保存后，才应用Cron时段。')
        st.caption('示例：30 19 * * mon-fri＝周一至周五19:30；0 9,15 * * mon,wed,fri＝周一、三、五09:00和15:00；*/30 9-15 * * mon-fri＝周一至周五09点至15点每半小时。')
        st.caption('到点后有5分钟发送窗口，跨自然日不补发；后台繁忙或离线超过窗口时等下次。没有新的有效提醒就不发送，不会按Cron重复已有通知。Cron不自动排除法定节假日。')
        if not channel['enabled']:
            st.caption('此Webhook总开关当前关闭；即使到点也不会发送。')
    summary.caption('发送时间：'+('配置无效，正式推送暂缓' if config.get('error') else '按Cron定时发送（北京时间）' if config['enabled'] else '即时发送'))
