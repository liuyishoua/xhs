# 手机验证码转交

手机Webhook使用标准HTTPS入口`https://portal.example.com/mobile-sms/inbound/{user_id}`（443端口），由Caddy仅代理此路径到工作台。旧20036端口接口保留。手机号、签名和正文不放入URL。默认消息模板中的日期、来源号码和卡槽字段不参与验证码候选提取；验证码须与明确的验证码文字标签相邻。

`/api/sms-relay/setup` 是手机配置页；用原工作台账号登录后读取自己的WebServer地址及专用Secret。SmsForwarder发送通道使用Webhook、POST，WebParams留空，消息模板保留默认。设置Secret后，表单携带timestamp、sign和content。签名兼容SmsForwarder的HMAC-SHA256规则，五分钟过期；不接受浏览器Origin。配置接口需要工作台Bearer凭据并禁止缓存。

手机规则只匹配小红书验证码短信。设置页显示最后收到时间及处理状态，不显示短信正文或验证码。私有`sms-relay.sqlite3`保存加密Secret、加密手机号、短期登录请求及摘要回执，不保存验证码和短信正文。

正文不含平台名称时，在设置页登记实际的小红书短信发送号码或名称，并将手机规则改为按完整来源号码匹配。Webhook默认表单的from字段用于服务器精确匹配；允许的发送来源按工作台用户隔离。正文仍须包含验证码标识，且必须存在唯一有效的待登录任务，才会提交验证码。未登记的来源不能仅凭一串数字触发登录。

第一次先使用发送通道的测试发送核验连接；没有等待中的登录任务时只更新收到状态，不尝试登录。已有工作台的PC/Creator短信发送接口在明确指定自己的账号且已配置手机时登记等待请求。每个工作台用户只保留一个有效等待请求，五分钟内的匹配验证码只能占位一次。占位后通过既有登录服务核对身份和更新Cookie。执行中断、验证码拒绝或额外验证进入needs_attention，不盲目再次提交验证码。

当前未启用自动检测掉线后发送短信。需要先完成真实手机转发验证，再单独配置账号对应手机号及续登触发策略。扩展其他平台需增加各平台的登录适配和准确请求绑定。

回归测试用临时目录、合成短信及fake登录服务，不发送真实短信或登录真实账号：

```bash
.venv/bin/python -m pytest tests/backend/test_sms_relay.py tests/backend/test_pc_qr_login.py tests/backend/test_account_renewal.py -q
```
