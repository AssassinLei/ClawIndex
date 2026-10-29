"""Plain-language decision context; never infer transactions or position sizes."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


FOLLOWUP = {'SUBMITTED','PARTIAL','DEFERRED'}


def handling(status, action):
    pending = '当前退出条件仍成立，' if action=='SELL' else '当前入场条件仍成立，' if action=='BUY' else ''
    messages = {
        'SUBMITTED':('已提交，待成交确认','你只确认了已提交订单，尚未确认成交。请先核对实际基金订单，确认前请勿重复下单。'),
        'PARTIAL':('部分成交，待跟进','你只确认了部分成交，剩余部分尚未确认。先核对实际订单，再更新处理结果；请勿重复下单。'),
        'DEFERRED':('已暂缓，尚未成交',f'你选择暂不执行，没有据此确认成交。{pending}可在下方更新处理结果；若已自行报单，请勿重复下单。'),
        'EXECUTED':('已确认成交','你已确认本次操作成交。请勿重复下单。'),
        'CANCELLED':('已记录取消','已记录取消这条操作，没有据此确认成交。再次行动前先核对当前判断和实际订单。'),
        'EXPIRED':('已记录过期','这条操作已记录为过期，没有据此确认成交。再次行动前先核对当前判断和实际订单。'),
    }
    title,text = messages.get(status,('已记录反馈','已保存你的反馈；反馈本身不代表实际成交。'))
    return {'title':title,'text':text,'followup':status in FOLLOWUP and action in ('BUY','SELL')}


def plan_context(signal, decision):
    """Separate an old trigger from still-active conditions and from a fill."""
    action = decision.get('action')
    event = signal.get('event')
    if action=='BUY':
        event_text = '本次行情出现入场触发。' if event=='BUY' else '当前入场条件仍成立，这是追随已有条件，不是今天又出现了新的交叉信号。'
    elif action=='SELL':
        event_text = '本次行情出现退出触发。' if event=='SELL' else '当前退出条件仍成立；系统没有据此认定你已经卖出。'
    else:
        event_text = ''
    trigger = signal.get('triggered_at')
    if trigger and event_text:
        event_text += ' 规则最近触发：'+trigger+'。'
    expiry = decision.get('expires_at')
    if expiry:
        try:
            expiry = datetime.fromisoformat(expiry).astimezone(ZoneInfo('Asia/Hong_Kong')).strftime('%Y-%m-%d %H:%M')
        except (TypeError,ValueError):
            expiry = None
    validity = ('判断有效期至 '+expiry+'（北京时间）；' if expiry else '')+'行情、规则或你报告的持仓更新时，旧判断可能提前失效，以当前页面为准。'
    return {'event':event_text,'validity':validity}


def summary_counts(items, awaiting=0):
    """A deferred decision and an unfilled order must not mean 'nothing to do'."""
    counts = {'action':0,'quiet':0,'waiting':awaiting,'followup':0}
    for item in items:
        if item['category']=='waiting':
            counts['waiting']+=1
        elif item['category']=='action':
            counts['action']+=1
        else:
            status = item['feedback'][0]['status'] if item.get('feedback') else None
            actionable = item.get('decision',{}).get('action') in ('BUY','SELL')
            if actionable and status=='DEFERRED':
                counts['action']+=1
                counts['followup']+=1
            elif actionable and status in ('SUBMITTED','PARTIAL'):
                counts['waiting']+=1
                counts['followup']+=1
            else:
                counts['quiet']+=1
    return counts


def display_priority(item):
    """Put undecided exits first, without turning an outstanding order into a new trade."""
    decision = item['decision']
    status = item['feedback'][0]['status'] if item.get('feedback') else None
    if item.get('current') and decision.get('status')=='AVAILABLE' and decision.get('action')=='SELL' and (not item.get('handled') or status=='DEFERRED'):
        return 0
    if item['category']=='action':
        return 1
    if item['category']=='waiting':
        return 2
    if status in ('SUBMITTED','PARTIAL'):
        return 3
    if status=='DEFERRED':
        return 4
    return 5


def research_reuse_message(receipt, run_id, clicked_at):
    """Only a receipt produced by this click can describe a reused experiment."""
    if not receipt or receipt.get('status')!='REUSED' or receipt.get('run_id')!=run_id:
        return None
    try:
        requested = datetime.fromisoformat(receipt['requested_at'])
        original = datetime.fromisoformat(receipt['original_requested_at'])
        local = ZoneInfo('Asia/Hong_Kong')
        if requested.date() is None or requested.tzinfo is None or original.tzinfo is None:
            return None
        if requested.astimezone(local).date()!=clicked_at.astimezone(local).date() or not clicked_at<=requested<=clicked_at+timedelta(minutes=5):
            return None
    except (KeyError,TypeError,ValueError):
        return None
    return ('行情与规则未变，复用 '+original.astimezone(local).strftime('%Y-%m-%d %H:%M')+
            ' 已检验结果，本次未重新搜索或改参数。行情截至 '+str(receipt.get('as_of') or '原记录日期')+'。')
