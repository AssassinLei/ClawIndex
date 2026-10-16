"""Per-user channel filters and minimal Feishu notification messages."""
import re
from urllib.parse import urlsplit


MODES = {'OFF':'关闭','ALL':'开启（全部操作）','BUY_ONLY':'只推送买入','BUY_SELL':'只推送买入、卖出'}
ACTIONS = {'ALL':{'BUY','SELL','HOLD','WAIT'},'BUY_ONLY':{'BUY'},'BUY_SELL':{'BUY','SELL'},'OFF':set()}
ACTION_LABELS = {'BUY':'买入','SELL':'卖出','HOLD':'继续持有','WAIT':'空仓等待'}


def validate_url(url):
    if not isinstance(url,str):
        raise ValueError('请输入飞书Webhook地址')
    url = url.strip()
    try:
        parts = urlsplit(url)
        valid = (parts.scheme=='https' and parts.hostname=='open.feishu.cn' and parts.port in (None,443)
                 and not parts.username and not parts.password and not parts.query and not parts.fragment
                 and re.fullmatch(r'/open-apis/bot/v2/hook/[A-Za-z0-9_-]+',parts.path))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('仅支持标准飞书机器人 HTTPS Webhook')
    return url


def filter_key(user_id, channel_id):
    return f'webhook_filter:{user_id}:{channel_id}'


def channel_mode(connection, channel_id, user_id, enabled):
    if not enabled:
        return 'OFF'
    row = connection.execute('SELECT value FROM settings WHERE key=?',(filter_key(user_id,channel_id),)).fetchone()
    # Keep existing enabled channels at their old BUY/SELL-only behavior.
    mode = row[0] if row else 'BUY_SELL'
    return mode if mode in ACTIONS else 'OFF'


def allows(mode, action):
    return action in ACTIONS.get(mode,set())


def operation_payload(index_name, action):
    if action not in ACTION_LABELS or not isinstance(index_name,str) or not index_name.strip():
        raise ValueError('通知缺少有效的指数名称或操作')
    return {'index_name':index_name,'action':action}


def opportunity_payload(row, state):
    payload = {'radar_name':row['name'],'radar_state':state}
    if row.get('level') == 'L2':
        payload.update(radar_level='L2',radar_parent=row.get('parent_index'))
    return payload


def message_body(payload):
    if 'radar_name' in payload:
        names = {'READY':'研究入场条件满足','WATCH':'新增观察机会','WITHDRAWN':'机会已撤销'}
        if not isinstance(payload['radar_name'],str) or not payload['radar_name'].strip() or payload.get('radar_state') not in names:
            raise ValueError('机会提醒缺少有效名称或状态')
        text = '机会雷达｜'+names[payload['radar_state']]
        if payload.get('radar_level') == 'L2':
            parent = payload.get('radar_parent')
            if parent is not None and (not isinstance(parent,dict) or set(parent)!={'name','code'} or
                                       not all(isinstance(parent[k],str) and parent[k].strip() for k in ('name','code'))):
                raise ValueError('机会提醒的一级行业资料无效')
            text += '\n所属一级行业：'+(parent['name']+'（'+parent['code']+'）' if parent else '暂未确认')
        text += '\n研究观察，不是买入指令。操作请查看系统。'
        return {'msg_type':'interactive','card':{
            'header':{'title':{'tag':'plain_text','content':payload['radar_name']}},
            'elements':[{'tag':'div','text':{'tag':'plain_text','content':text}}],
        }}
    if 'index_name' in payload:
        message = operation_payload(payload['index_name'],payload.get('action'))
        return {'msg_type':'interactive','card':{
            'header':{'title':{'tag':'plain_text','content':message['index_name']}},
            'elements':[{'tag':'div','text':{'tag':'plain_text','content':'操作：'+ACTION_LABELS[message['action']]}}],
        }}
    return {'msg_type':'text','content':{'text':payload['text']}}
