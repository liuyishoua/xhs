"""Safe, explicit PC login diagnoses; transport failures are not logout proof."""


class AccountCheckError(ValueError):
    def __init__(self, account_status, message, business_code=None):
        super().__init__(message)
        self.account_status = account_status
        self.business_code = business_code


def creator_session_failure(code=None, http_status=None):
    if code == 906:
        return AccountCheckError('risk', f'创作者上传权限被平台拒绝（HTTP {http_status}，业务码 906）：账号存在风险，请重新登录创作者账号完成验证；已审稿保留，验证后自动继续', code)
    if code == -100 or http_status == 401:
        return AccountCheckError('expired', '创作者登录已失效，请重新登录创作者账号；恢复后自动继续发布', code)
    return AccountCheckError('unknown', '创作者上传权限检查暂未通过，将自动重试；已审稿保留', code)


class CreatorUploadError(RuntimeError):
    def __init__(self, failure):
        self.failure = failure
        self.account_error = creator_session_failure(failure.get('business_code'), failure.get('http_status'))
        super().__init__(str(self.account_error))


def listener_account_issue(status):
    """Expose an actionable account state without leaking check internals."""
    if status == 'active':
        return None
    if status == 'expired':
        return {'state': 'login_expired', 'account_status': status,
                'message': '网页登录已过期，当前无法读取评论通知或发送回复。请在账号矩阵重新登录；恢复后自动补收未处理留言。'}
    return {'state': 'account_unavailable', 'account_status': status,
            'message': '账号登录或验证状态未恢复，请在账号矩阵检查并完成平台验证；恢复后自动继续处理。'}


def profile_failure(payload):
    code = payload.get('code') if isinstance(payload, dict) else None
    if code == -100:
        return AccountCheckError('expired', '小红书登录已失效（业务码 -100），请在账号矩阵点击「重新登录」，通过手机验证码恢复登录', code)
    if code == 300012:
        return AccountCheckError('risk', '小红书提示 IP 存在风险，请检查家庭出口并在官方网页完成验证', code)
    return AccountCheckError('unknown', '小红书登录检查未通过，暂不能确认是否失效，请稍后重试', code)


def require_pc_session(payload):
    """Only explicit platform authentication codes establish a session failure."""
    if isinstance(payload, dict) and payload.get('success') is not True and payload.get('code') in (-100, 300012):
        raise profile_failure(payload)
