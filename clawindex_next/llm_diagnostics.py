"""Safe AI failure categories. Never persist raw HTTP bodies, prompts or secrets."""


MESSAGES = {
    'TIMEOUT': ('AI接口响应超时，请稍后重试。', True),
    'CONNECTION': ('无法连接AI接口，请检查服务器网络与接口地址后重试。', True),
    'NOT_CONFIGURED': ('尚未配置AI密钥，请在服务环境中配置后重试。', False),
    'INVALID_CONFIG': ('AI请求配置无效，请检查超时、输出长度或思考模式设置。', False),
    'AUTHENTICATION': ('AI接口认证失败，请检查密钥是否有效。', False),
    'INSUFFICIENT_BALANCE': ('AI服务账户余额不足，请检查服务商账户余额。', False),
    'PERMISSION': ('当前AI密钥没有调用此模型的权限。', False),
    'MODEL_NOT_FOUND': ('AI接口或模型不存在，请检查模型名与接口地址。', False),
    'RATE_LIMIT': ('AI接口限流或额度不足，请检查额度，或稍后重试。', True),
    'BAD_REQUEST': ('AI接口拒绝了请求参数，请检查模型与接口的兼容性。', False),
    'PROVIDER_UNAVAILABLE': ('AI服务暂时不可用，请稍后重试。', True),
    'INCOMPLETE_RESPONSE': ('AI回答未完整结束，本次未采用，可重试。', True),
    'EMPTY_RESPONSE': ('AI接口返回了空回答，本次未采用，可重试。', True),
    'INVALID_JSON': ('AI返回内容不是有效JSON，本次未采用，可重试。', True),
    'INVALID_STRUCTURE': ('AI回答的字段或格式不符合要求，本次未采用，可重试。', True),
    'UNKNOWN_EVIDENCE': ('AI引用了未提供的指标，本次未采用，可重试。', True),
    'UNSAFE_CLAIM': ('AI回答包含交易指令或收益保证，已拦截并保留程序解读。', True),
    'UNEXPECTED': ('AI解读发生未预期错误，请根据诊断编号排查。', True),
}


def failure_diagnostic(exc, phase):
    """Use types/status/our own codes, never str(exc) from a provider."""
    name = type(exc).__name__
    status = getattr(exc,'status_code',None)
    code = getattr(exc,'code',None)
    if not isinstance(code,str):
        code = None
    if phase == 'validation':
        code = code if code in ('INVALID_STRUCTURE','UNKNOWN_EVIDENCE','UNSAFE_CLAIM') else 'INVALID_JSON' if name == 'JSONDecodeError' else 'INVALID_STRUCTURE'
    elif name in ('APITimeoutError','TimeoutException','ReadTimeout','ConnectTimeout','TimeoutError'):
        code = 'TIMEOUT'
    elif name in ('APIConnectionError','ConnectError','ConnectionError'):
        code = 'CONNECTION'
    elif status in (401,402,403,404,429,400,422,408):
        code = {401:'AUTHENTICATION',402:'INSUFFICIENT_BALANCE',403:'PERMISSION',404:'MODEL_NOT_FOUND',429:'RATE_LIMIT',400:'BAD_REQUEST',422:'BAD_REQUEST',408:'TIMEOUT'}[status]
    elif isinstance(status,int) and status>=500:
        code = 'PROVIDER_UNAVAILABLE'
    elif code not in MESSAGES:
        code = {'LLM_NOT_CONFIGURED':'NOT_CONFIGURED','LLM_INVALID_CONFIG':'INVALID_CONFIG',
                'LLM_INCOMPLETE_RESPONSE':'INCOMPLETE_RESPONSE','LLM_EMPTY_RESPONSE':'EMPTY_RESPONSE'}.get(str(exc),'UNEXPECTED')
    message,retryable = MESSAGES[code]
    result = dict(code=code,message=message,retryable=retryable,phase=phase)
    if isinstance(status,int):
        result['http_status'] = status
    return result
