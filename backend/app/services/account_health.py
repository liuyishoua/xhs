"""Safe, explicit PC login diagnoses; transport failures are not logout proof."""


class AccountCheckError(ValueError):
    def __init__(self, account_status, message):
        super().__init__(message)
        self.account_status = account_status


def profile_failure(payload):
    code = payload.get('code') if isinstance(payload, dict) else None
    if code == -100:
        return AccountCheckError('expired', '小红书登录已失效（业务码 -100），请在账号矩阵点击「重新登录」，通过手机验证码恢复登录')
    if code == 300012:
        return AccountCheckError('risk', '小红书提示 IP 存在风险，请检查家庭出口并在官方网页完成验证')
    return AccountCheckError('unknown', '小红书登录检查未通过，暂不能确认是否失效，请稍后重试')
