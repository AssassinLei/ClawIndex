import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from .auth import Auth
from .engine import DEFAULT_SPEC
from .service import UserService
from .storage import Store, canonical
from .worker import worker_status
from .presentation import label, local_time, number, readable
from .ui_components import inspection_dialog, signal_dashboard, start_inspection
from .ui_evolution import evolution_panel, today_panel
from .ui_design import PAGES, install_design, brand, page_header, status_line, empty_state, navigate_button
from .webhooks import MODES


def guarded(fn):
    try:
        return fn()
    except (ValueError,PermissionError,RuntimeError,ImportError) as exc:
        st.error(str(exc))
        return None


def toggle_webhook_address(channel_id):
    key = 'show_webhook_address_'+str(channel_id)
    st.session_state[key] = not st.session_state.get(key,False)


def inspection_toolbar(svc,watches):
    with st.container(horizontal=True,vertical_alignment="center"):
        with st.popover("巡检选项"):
            saved_ai = svc.inspection_ai_enabled()
            include_ai = st.checkbox("巡检时同时生成AI解读",value=saved_ai,key='inspection_include_ai_'+str(svc.user['id']),help="此设置按账号保存，同时用于手动和后台自动巡检，关闭网页后仍生效。成功解读会复用；需要模型配置，可能消耗额度。")
            if include_ai != saved_ai:
                svc.set_inspection_ai(include_ai)
            st.caption("巡检会先检查数据，再生成操作判断。今日相同输入的结果会复用。")
        if st.button("提交全部巡检",type="primary",disabled=not watches,help="日常更新由后台自动完成；需要立即检查时可点击。"):
            guarded(lambda:start_inspection(svc,watches,include_ai))
    st.caption("程序产生买卖判断 · AI仅解释 · 数据与解读可在每张卡片内查看")


def run():
    load_dotenv(Path(__file__).resolve().parent.parent/".env.next")
    st.set_option("client.toolbarMode","minimal")
    st.set_page_config(page_title="ClawIndex · 投资助手",page_icon="📈",layout="wide")
    install_design()
    path = os.environ.get("VNEXT_DB_PATH",str(Path(__file__).resolve().parent.parent/"quant_system_next.db"))
    try:
        db = Store(path)
    except Exception:
        st.error("新版数据库未就绪或版本不匹配。请按新版运行手册进行独立迁移，应用不会创建或改写旧库。")
        st.stop()
    auth = Auth(db)
    if "next_token" not in st.session_state:
        _, center, _ = st.columns([1,2,1])
        with center:
            brand()
            st.title("让每天的投资判断更清楚")
            st.caption("关注指数，查看操作，按需了解原因。行情与研究交给后台。")
            with st.form("login"):
                username = st.text_input("账号名",placeholder="输入你常用的名字")
                submit = st.form_submit_button("登录",type="primary",width="stretch")
            if submit:
                token = guarded(lambda:auth.login(username))
                if token:
                    st.session_state.next_token = token
                    st.rerun()
            st.caption("输入账号名即可进入，新账号会自动创建。以后请使用同一个名字，找回自己的指数与记录。")
        return
    try:
        svc = UserService(db,st.session_state.next_token)
        user = svc.user
    except PermissionError:
        del st.session_state.next_token
        st.rerun()
    with st.sidebar:
        brand()
        st.write(f"当前用户：{user['username']}")
        page = st.radio("工作台",list(PAGES),format_func=lambda key:PAGES[key][0],key="main_page",label_visibility="collapsed")
        st.divider()
        online = bool(worker_status(db))
        status_line(online,"后台运行中" if online else "后台未连接")
        st.caption("行情自动更新："+("工作日开启" if svc.daily_sync_enabled() else "已暂停"))
        st.caption("系统生成判断，交易由你执行。")
        if not online:
            st.caption("后台恢复后会继续处理任务。可到“系统状态”排查。")
        if st.button("退出登录",width="stretch"):
            auth.logout(st.session_state.next_token)
            st.session_state.clear()
            st.rerun()
    page_header(page)
    notice = st.session_state.pop("inspection_notice",None)
    if notice:
        if notice["success"]:
            st.success(f"已完成 {notice['success']} 项更新/巡检，下方结果已刷新。")
        for error in notice["errors"]:
            st.error(error)
    ai_notice = st.session_state.pop('ai_notice',None)
    if ai_notice:
        (st.success if ai_notice['ok'] else st.warning)(ai_notice['message'])
    watches = svc.watchlist()
    selected = None
    if watches and page not in ("信号与待执行","今日操作","通知与运行","健康","历史与反馈","发现机会"):
        selected = st.selectbox("监控指数",watches,format_func=lambda a:f"{a['name']} · {a['code']}")
    if page=='发现机会':
        from .ui_radar import radar_panel
        radar_panel(svc,guarded)
    elif page == "信号与待执行":
        inspection_toolbar(svc,watches)
        signal_dashboard(svc)
    elif page == "今日操作":
        inspection_toolbar(svc,watches)
        today_panel(svc,guarded)
        with st.expander("第一次使用？只需了解这三件事"):
            st.write("1. 在“我的指数”添加关注对象，行情缺口由后台自动补齐。")
            st.write("2. 实际持有时，到“我的持仓”登记；未登记默认空仓。")
            st.write("3. 每天看这里的待办，按需查看数据或解读；解读会标明是AI还是程序生成。买卖后可记录处理结果。")
        navigate_button("查看全部信号详情", "信号与待执行", "home_signals")
    elif page == "监控与数据":
        with st.expander("＋ 添加关注的指数",expanded=not bool(watches)):
            keyword = st.text_input('搜索指数',placeholder='输入名称或代码，例如：电池、机器人、恒生、801730',key='watch_search')
            matches = svc.search_indices(keyword)
            if matches:
                choice = st.selectbox('搜索结果',matches,index=None,format_func=lambda item:f"{item['name']} · {item['code']}",placeholder='选择要添加的指数',key='watch_search_choice')
                if st.button('添加监控',type='primary',disabled=choice is None):
                    if guarded(lambda:svc.add_watch(choice['code'])):
                        st.session_state.watch_notice = '已关注：'+choice['name']+'，后台会自动维护行情。'
                        st.rerun()
            else:
                st.info('未找到匹配指数，可换个关键词或直接输入代码。申万行业目录由后台自动更新。')
            with st.expander('直接输入指数代码'):
                with st.form('add_watch'):
                    code = st.text_input('指数代码',placeholder='例如 801730.SI 或 HKTECH')
                    if st.form_submit_button('按代码添加'):
                        if guarded(lambda:svc.add_watch(code)):
                            st.session_state.watch_notice = '已关注：'+code.strip().upper()
                            st.rerun()
        if st.session_state.get('watch_notice'):
            st.success(st.session_state.pop('watch_notice'))
        if selected:
            cols = st.columns(3)
            cols[0].metric("关注数量",len(watches))
            cols[1].metric("当前市场",label(selected['market']))
            cols[2].metric("持仓状态",label(svc.position(selected['code'])['status']))
            st.caption("历史行情、基础指标与适用的专业因子均由后台自动维护，无需手动补数据。")
            from .ui_trends import trend_expander
            trend_expander(svc,selected['code'],'watch')
            if st.button("提交该指数巡检",type="primary"):
                guarded(lambda:start_inspection(svc,[selected]))
            from .ui_components import data_repair_controls
            data_repair_controls(svc,selected,'watch_repair_'+selected['code'])
            with st.expander("标的资料与来源"):
                st.json(json.loads(selected["metadata"]))
            with st.expander("后台数据维护进度"):
                for key in ('history_repair:','factor_sync:'):
                    records = svc.store.rows('SELECT value FROM settings WHERE key=?',(key+selected['code'],))
                    if records:
                        detail = json.loads(records[0]['value'])
                        st.caption(('历史行情' if key.startswith('history') else '专业因子')+'：'+('后台分批补充中' if detail.get('pending') else '本轮检查完成'))
                        if 'missing_sessions' in detail:
                            st.caption(f"待核验时段 {detail['missing_sessions']} 个；接口未提供的时段保留缺口，后台定期重试。")
            with st.expander("高级维护（通常无需使用）"):
                start = st.text_input("补历史起始日期（留空自动选择）",placeholder="2016-01-01")
                if st.button("从Tushare同步"):
                    guarded(lambda:start_inspection(svc,[selected],kind="sync",start=start.strip() or None))
            with st.expander("管理此指数"):
                st.caption("停止关注后不再为此指数生成新判断，已有历史记录会保留。")
                if st.button("停止监控并保留历史"):
                    svc.unwatch(selected["id"])
                    st.rerun()
                st.caption('停止监控也会停止该指数的待发送通知，并在机会雷达暂不关注它；以后可在“发现机会”恢复关注。')
    elif page == "持仓与预算" and selected:
        st.write("未登记默认空仓；真实持有时改为持有。状态保留到你修改或确认成交，无需每周复核，金额可留空。")
        old = svc.position(selected["code"])
        if old:
            st.write("当前按默认空仓判断（尚未登记真实持仓）" if old.get('basis') == 'DEFAULT_EMPTY' else f"上次登记：{local_time(old['reported_at'])}；状态：{label(old['status'])}")
            if old.get("target_completion") is not None:
                st.write(f"历史目标投入完成度：{old['target_completion']:.1%}（待复核）")
        with st.form("position"):
            status = st.selectbox("确认当前持仓状态",["UNKNOWN","EMPTY","HELD"],index=["UNKNOWN","EMPTY","HELD"].index(old["status"] if old else "UNKNOWN"),format_func=label)
            with st.expander("可选：补充市值、份额与备注"):
                value = st.text_input("实际基金当前市值（可留空）")
                shares = st.text_input("实际基金份额（可留空）")
                currency = st.selectbox("金额币种",["CNY","HKD","USD"],format_func=label)
                note = st.text_input("备注（可留空）")
            if st.form_submit_button("确认并追加持仓报告",type="primary"):
                fields = {"note":note}
                if value.strip(): fields.update(market_value=value,currency=currency)
                if shares.strip(): fields["shares"] = shares
                if guarded(lambda:svc.report_position(selected["code"],status,fields)):
                    start_inspection(svc,[selected])
                    st.success("已记录确认时间。阅读信号或填写执行反馈不会自动更新持仓。")
        with st.expander("可选：预算与真实基金映射"):
            with st.form("budget"):
                limit = st.text_input("该基金投入上限")
                used = st.text_input("已投入本金")
                cash = st.text_input("本次可用现金")
                bcurrency = st.selectbox("预算币种",["CNY","HKD","USD"],format_func=label)
                if st.form_submit_button("保存预算"):
                    guarded(lambda:svc.set_budget(selected["code"],dict(limit=limit,used=used,cash=cash,currency=bcurrency)))
            with st.form("mapping"):
                product = st.text_input("实际基金代码")
                source = st.text_input("基金官方资料HTTPS链接")
                pcurrency = st.selectbox("实际基金交易币种",["CNY","HKD","USD"],format_func=label)
                verified = st.checkbox("我已核实该基金跟踪此指数及交易币种")
                if st.form_submit_button("保存基金映射"):
                    guarded(lambda:svc.set_mapping(selected["code"],dict(product_code=product,currency=pcurrency,source_url=source,verified=verified)))
    elif page == "策略与研究" and selected:
        from .ui_assessment import assessment_panel
        assessment_panel(svc,selected['code'])
        evolution_panel(svc,selected,guarded)
        with st.expander("高级：手动研究与规则配置"):
            from .factors import PROFILES
            current_spec = json.loads(selected['spec'])
            options = ['NONE',*PROFILES]
            choice = st.selectbox('入场因子组合',options,index=options.index(current_spec.get('factor_profile','NONE')),format_func=lambda k:PROFILES[k]['label'] if k in PROFILES else '原有均线趋势规则')
            st.write("规则：连续确认高于均线且均线向上才入场；连续确认低于均线退出。估值不能临时阻止退出。")
            ma = st.number_input("均线交易日数",20,250,int(json.loads(selected["spec"])["ma"]))
            confirm = st.number_input("连续确认日数",1,10,int(json.loads(selected["spec"])["confirmation"]))
            slope = st.number_input("均线斜率跨度",1,20,int(json.loads(selected["spec"])["slope_window"]))
            spec = {**current_spec,"ma":int(ma),"confirmation":int(confirm),"slope_window":int(slope)}
            if choice != 'NONE':
                spec.update(name='trend_factor_v2',factor_profile=choice)
            else:
                spec['name'] = 'trend_confirm_v1'
                spec.pop('factor_profile',None)
            goal = st.text_input("研究目标",value="控制回撤并记录收益代价")
            if st.button("运行成本、延迟与邻近参数研究"):
                with st.spinner("使用同一个规则引擎逐日回放；不自动选取或启用最赚钱参数"):
                    result = guarded(lambda:svc.research(selected["code"],spec,goal))
                    if result:
                        st.session_state.research_report = {"user":user["id"],"code":selected["code"],"report":result[1]}
            saved = st.session_state.get("research_report")
            if saved and saved["user"] == user["id"] and saved["code"] == selected["code"]:
                report = saved["report"]
                if not report["research_usable"]:
                    st.warning("研究存在历史缺口或评估期不足一年，以下为诊断性结果，不能用于确认策略优势或开启正式通知。请补全历史后重跑。")
                st.dataframe(pd.DataFrame([{"均线天数":r["spec"]["ma"],"单边费用（万分之）":r["cost_bps_per_side"],"成交延迟（交易日）":r["execution_delay_sessions"],"策略收益":number(r["strategy"]["return"],True),"最大回撤":number(r["strategy"]["max_drawdown"],True),"持续持有收益":number(r["buy_hold"]["return"],True),"交易次数":r["trade_count"]} for r in report["results"]]),hide_index=True)
                chosen = next((r for r in report["results"] if r["spec"] == spec and r["cost_bps_per_side"] == 10 and r["execution_delay_sessions"] == 1),report["results"][0])
                st.line_chart(pd.DataFrame(chosen["curve"]).set_index("date").rename(columns={"strategy":"规则策略","buy_hold":"持续持有"}).rename_axis("日期"))
                for text in chosen["limitations"]:
                    st.caption(readable(text))
                with st.expander("不同时间区间的表现"):
                    names = {"development":"开发区间","validation":"验证区间","held_out_test":"测试区间"}
                    st.dataframe(pd.DataFrame([{"区间":names[k],"开始":v["start"],"结束":v["end"],"策略收益":number(v["strategy"]["return"],True),"持有收益":number(v["buy_hold"]["return"],True)} for k,v in chosen["splits"].items() if v]),hide_index=True)
                with st.expander("按已观察到的市场特征分组（非牛熊预测）"):
                    st.dataframe(pd.DataFrame([{"市场特征":label(k),"交易日数":v["sessions"],"策略平均日收益":number(v["strategy_mean_daily_return"],True),"持有平均日收益":number(v["buy_hold_mean_daily_return"],True)} for k,v in chosen["regime_performance"].items()]),hide_index=True)
                st.download_button("下载完整研究报告",canonical(report),file_name="index-research.json",mime="application/json")
            stage = st.selectbox("规则使用阶段",["RESEARCH","SHADOW","ENABLED"],index=["RESEARCH","SHADOW","ENABLED"].index(selected["stage"]),format_func=label)
            catchup = st.checkbox("空仓时允许追随当前仍有效的入场条件（非新信号）",value=bool(selected['catchup']))
            accepted = st.checkbox("我已审阅上述规则与研究限制；启用不代表有收益保证")
            if st.button("发布新规则版本并应用"):
                if stage == "ENABLED" and not accepted:
                    st.error("启用前需要审阅和确认规则及研究限制。")
                elif guarded(lambda:svc.assign(selected["code"],spec,stage,catchup)):
                    st.rerun()
    elif page == "历史与反馈":
        history = svc.history()
        st.header("历史判断")
        names = {w['code']:w['name'] for w in watches}
        code_filter = st.selectbox("筛选指数",[None,*sorted({r['code'] for r in history})],format_func=lambda code:"全部指数" if code is None else names.get(code,code))
        if code_filter:
            history = [r for r in history if r['code']==code_filter]
        st.caption("判断会自动留存。实际处理结果由你记录，系统信号不代表真实成交；停止关注的指数仍可查询。")
        if not history:
            empty_state("还没有操作记录", "首次巡检完成后，判断会自动保存在这里。")
        st.dataframe(pd.DataFrame([{"行情日期":r["as_of"],"指数":names.get(r["code"],r["code"]),"指数状态":label(json.loads(r["signal"])["state_label"]),"个人判断":label(json.loads(r["decision"])["action"] or json.loads(r["decision"])["status"])} for r in history]),hide_index=True,width="stretch")
        if history:
            with st.expander('查看某次巡检的历史曲线',key='history_curves',on_change='rerun') as curves:
                if curves.open:
                    from .ui_trends import trend_panel
                    signals = {r['signal_id']:r for r in history}
                    signal_id = st.selectbox('选择巡检记录',list(signals),format_func=lambda sid:f"{signals[sid]['as_of']} · {names.get(signals[sid]['code'],signals[sid]['code'])} · {sid[:8]}",key='history_chart_signal')
                    trend_panel(svc,signals[signal_id]['code'],'history',signal_id)
            st.header("补记处理结果")
            record = st.selectbox("反馈对应记录",history,format_func=lambda r:f"{r['as_of']} {r['code']} {label(json.loads(r['decision'])['action'] or json.loads(r['decision'])['status'])}")
            status = st.selectbox("执行反馈状态",["ACKNOWLEDGED","EXECUTED","PARTIAL","DEFERRED","EXPIRED","CANCELLED"],format_func=label)
            note = st.text_input("执行情况或暂缓原因")
            if st.button("追加执行反馈"):
                if guarded(lambda:svc.feedback(record["decision_id"],status,note)):
                    st.success("反馈已保存；请另行确认实际持仓状态。")
        with st.expander("旧版归档（未证明当时输入完整，不能作为新版回测）"):
            legacy = svc.legacy()
            st.write(f"归档记录{len(legacy)}条。旧置信度不是概率，旧提示词没有直接转为交易规则。")
            st.dataframe(pd.DataFrame([{"类型":r["table_name"],"原始记录":r["payload"]} for r in legacy]))
        st.download_button("导出新增持仓与反馈（回滚时保留）",canonical(svc.export_feedback()),file_name="positions-feedback.json",mime="application/json")
    elif page == "通知与运行":
        st.header("我的 Webhook")
        st.caption("这里只管理当前账号的地址。每条Webhook独立设置，按针对你的操作判断筛选；正式通知仅包含指数名称和操作。")
        notice = st.session_state.pop('webhook_notice',None)
        if notice:
            st.success(notice)
        channels = svc.channels()
        if not channels:
            st.info("尚未添加提醒渠道。你仍可在“今日操作”查看所有判断。")
        for channel in channels:
            with st.container(border=True):
                st.write(channel['label'] or f"Webhook {channel['id']}")
                mode = st.selectbox("推送方式",list(MODES),index=list(MODES).index(channel['mode']),format_func=MODES.get,key='channel_mode_'+str(channel['id']))
                if mode!=channel['mode'] and guarded(lambda:svc.configure_channel(channel['id'],mode)):
                    st.success('已保存：'+MODES[mode])
                from .radar_notifications import MODES as RADAR_MODES
                radar_mode = svc.radar_channel_mode(channel['id'])
                radar_on = st.checkbox('开启机会推送',value=radar_mode!='OFF',key='radar_on_'+str(channel['id']),help='只控制这个Webhook的机会提醒，其他Webhook不受影响。关闭后取消尚未发送的机会提醒。')
                scopes = [key for key in RADAR_MODES if key!='OFF']
                radar_scope = st.selectbox('机会推送范围',scopes,index=scopes.index(radar_mode) if radar_mode in scopes else 0,format_func=RADAR_MODES.get,key='radar_scope_'+str(channel['id']),disabled=not radar_on)
                radar_choice = radar_scope if radar_on else 'OFF'
                if radar_choice!=radar_mode and guarded(lambda:svc.set_radar_channel(channel['id'],radar_choice)):
                    st.success('机会提醒设置已保存。')
                st.caption('只推送当前仍有效、符合个人筛选偏好的机会，不推送撤销。同一行情日相同机会不重复发送；观察升级为条件满足时可补发一次。上方渠道关闭时也不发送。')
                if radar_on and mode=='OFF':
                    st.warning('机会推送已配置，但此Webhook总开关仍关闭。请先在上方“推送方式”开启渠道，才会实际发送。')
                from .ui_webhook_schedule import schedule_editor
                schedule_editor(svc,channel,guarded)
                reveal_key = 'show_webhook_address_'+str(channel['id'])
                with st.container(horizontal=True):
                    test_saved = st.button('发送测试消息',key='channel_test_'+str(channel['id']))
                    st.button('隐藏地址' if st.session_state.get(reveal_key) else '查看地址',key='channel_view_'+str(channel['id']),on_click=toggle_webhook_address,args=(channel['id'],))
                    with st.popover('删除',key='channel_delete_menu_'+str(channel['id'])):
                        st.write('删除此Webhook后停止推送，历史投递记录保留。')
                        if st.button('确认删除',key='channel_delete_'+str(channel['id']),type='primary'):
                            if guarded(lambda:svc.delete_channel(channel['id'])):
                                st.session_state.pop(reveal_key,None)
                                st.session_state.webhook_notice = 'Webhook已删除，待发送通知已取消。'
                                st.rerun()
                if st.session_state.get(reveal_key):
                    address = guarded(lambda:svc.channel_url(channel['id']))
                    if address:
                        st.code(address,language=None)
                if test_saved:
                    with st.spinner('正在发送“你好，世界”…'):
                        if guarded(lambda:svc.test_channel(channel['id'])):
                            st.success('测试消息“你好，世界”已发送。')
        st.caption("“全部操作”包括买入、卖出、继续持有和空仓等待。正式通知仍要求规则已启用提醒、判断有效；测试消息不受筛选或后台状态影响。")
        st.caption('飞书机器人若开启自定义关键词校验，请在允许的关键词中分别添加“你好，世界”（测试消息）和“操作”（正式卡片）。')
        with st.expander("添加飞书 Webhook",expanded=not bool(channels)):
            with st.form("channel"):
                url = st.text_input("飞书机器人通知地址",type="password",help='填写后可先发送测试消息，无需保存，也无需开启正式推送。')
                channel_label = st.text_input("渠道名称",placeholder="例如：我的投资提醒")
                new_mode = st.selectbox('新Webhook推送方式',list(MODES),format_func=MODES.get)
                from .radar_notifications import MODES as RADAR_MODES
                new_radar_on = st.checkbox('新Webhook开启机会推送',value=False,help='默认不推送机会；Webhook推送方式为关闭时，全部通知暂停。')
                new_scopes = [key for key in RADAR_MODES if key!='OFF']
                new_radar_scope = st.selectbox('新Webhook机会推送范围',new_scopes,format_func=RADAR_MODES.get)
                from .webhook_schedule import DEFAULT_CRON
                new_scheduled = st.checkbox('新Webhook按Cron定时发送',value=False)
                new_cron = st.text_input('新Webhook Cron表达式',value=DEFAULT_CRON,help='北京时间；标准五字段：分 时 日 月 周。默认示例为周一至周五19:30。测试消息不受时段限制。')
                with st.container(horizontal=True):
                    test = st.form_submit_button('测试：发送你好，世界')
                    save = st.form_submit_button('保存Webhook',type='primary')
                if test:
                    with st.spinner('正在发送“你好，世界”…'):
                        if guarded(lambda:svc.test_webhook(url)):
                            st.success('测试消息“你好，世界”已发送；此地址尚未保存。')
                if save:
                    if guarded(lambda:svc.set_channel(url,channel_label,mode=new_mode,radar_mode=new_radar_scope if new_radar_on else 'OFF',schedule={'enabled':new_scheduled,'cron':new_cron})):
                        st.session_state.webhook_notice = 'Webhook已保存：'+MODES[new_mode]+('；机会推送已配置开启（渠道关闭时暂停）' if new_radar_on else '；机会推送关闭')
                        st.rerun()
        if user["admin"]:
            st.header("后台自动运行")
            from .radar import enabled as radar_enabled
            radar_auto = st.checkbox('自动扫描L1、L2指数发现机会',value=radar_enabled(db),help='共享行情与筛选证据，后台分批补历史；不自动添加监控或下单。')
            if radar_auto!=radar_enabled(db):
                guarded(lambda:svc.set_radar_enabled(radar_auto))
            daily = st.checkbox("工作日自动更新所有用户关注的指数",value=svc.daily_sync_enabled(),help="每天按市场已发布时段更新，共享指数只拉取一次。关闭网页后，只要后台运行就会继续更新。")
            if daily != svc.daily_sync_enabled():
                svc.set_daily_sync(daily)
            enabled = db.rows("SELECT value FROM settings WHERE key='scheduler_enabled'")[0]["value"] == "true"
            choice = st.checkbox("额外开启定时巡检（保留原配置）",value=enabled,help="自动数据维护本身已会生成判断；此开关保留旧版额外巡检设置。通知仍由规则阶段和渠道开关决定。")
            if choice != enabled:
                svc.set_scheduler(choice)
        st.info("后台持续运行时才能自动更新与巡检。自动行情更新默认开启，自动巡检保留原设置，消息渠道由你单独开启。")
        st.caption('本账号自动巡检AI解读：'+('开启' if svc.inspection_ai_enabled() else '关闭')+'。可在“今日操作”的“巡检选项”修改，保存后自动巡检也会使用。')
        ai_pause = svc.ai_availability()
        if ai_pause:
            st.info(ai_pause['message']+' 自动AI请求暂停至 '+local_time(ai_pause['retry_at'])+'；程序判断继续运行。恢复额度或修改配置后，可在指数详情单独重试AI。')
    elif page == "健康":
        if worker_status(db):
            st.success("后台服务最近10分钟内有运行心跳。页面的任务列表需要刷新后查看最新状态。")
        else:
            st.warning("未检测到后台服务心跳。Pending＝等待执行；后台恢复后会继续处理。云服务器请检查 worker 服务，本地请使用一键启动。")
        from .ui_components import background_status_panel
        background_status_panel(svc)
        st.write("提交 → 后台接收 → 更新行情 → 分析 → 按设置生成AI解读 → 完成。失败任务会说明原因并最多尝试3次。")
        if st.button("刷新运行状态",type="primary"):
            st.rerun()
        with st.expander("高级排查与临时恢复"):
            st.caption("Linux 云服务器可运行 docker compose ps 检查服务，再查看 docker compose logs --tail=100 worker。正常使用无需手动执行任务。")
            manual = st.button("立即处理我的待执行巡检")
        if manual:
            with st.spinner("处理本账号任务，需要更新行情的请求会先同步，请稍候…"):
                count = guarded(svc.process_pending)
            if count is not None:
                st.success(f"已处理{count}项任务，请查看结果或错误说明；此按钮不会投递通知。")
        st.header("最近运行记录")
        st.dataframe(pd.DataFrame([{"模块":label(r["component"]),"状态":label(r["status"]),"说明":readable(r["detail"]),"时间":local_time(r["created_at"])} for r in svc.health()]),hide_index=True)
        with st.expander("任务明细"):
            st.dataframe(pd.DataFrame([{"指数":r["name"],"任务状态":r.get('queue_title') or label(r["status"]),"当前阶段":label(r["progress"]["phase"]),"说明":r.get('queue_hint') or r["error"] or r["progress"]["detail"],"更新时间":local_time(r["updated_at"])} for r in svc.jobs()]),hide_index=True)
    if not watches and page not in ("监控与数据","通知与运行","健康","发现机会"):
        if page != "今日操作":
            empty_state("先添加一个关注的指数", "添加后即可管理持仓、查看策略与历史判断。")
            navigate_button("去添加指数", "监控与数据", "empty_watch")
    if st.session_state.get("loading_jobs"):
        inspection_dialog(svc)
