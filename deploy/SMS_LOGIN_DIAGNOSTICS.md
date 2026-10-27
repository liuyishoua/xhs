# PC 手机验证码登录诊断

PC 工作台入口调用 SDK 时必须先执行 `generate_init_cookies()`，随后执行 `ensure_webprofile(cookies)`，最后才执行 `send_phone_code()`。SDK 自带手机号登录入口也是这个顺序；不应跳过匿名 profile 上报。

当前工作台在用户点击发码之后冷启动登录环境。只读实测（未发送短信）初始化包含 10 次 HTTP 请求，合计约 14 秒；官方页面通常在页面加载阶段就完成了准备，不能直接用点击后的等待时长比较两者。该观察没有证明 Wi-Fi 绑定造成延迟，也没有测量平台短信下发耗时。

PC 发码请求将诊断写入 `api_logs`，endpoint 为 `internal/xhs/pc/phone/send-code`。字段包括 `phase`、`connection_wait_ms`、`initialization_ms`、`send_request_ms`、`total_ms`、`platform_code`、`platform_success`，失败时仅附异常类型。不记录手机号、短信码、Cookie、签名或平台完整响应。

平台接口接受请求与手机收到短信是两种状态。页面仅说明“小红书已接受验证码请求”；会话 pending 表示等待输入短信验证码，不能证明手机送达。历史请求未保存这些诊断时，不能倒推其实际平台返回码或每个阶段耗时。

默认回归使用 fake SDK，不发送真实短信。手机送达验证需用户手动发码并确认；诊断中不自动重发。源码回归：`tests/backend/test_pc_phone_preflight.py`，以及现有手机号登录和重新登录 API 检查。

## PC 登录预准备

`POST /api/xhs/login-sessions/pc/phone/prepare` 在打开短信登录面板时执行匿名初始化及 webprofile；不需要手机号，也不会发短信。返回的随机令牌仅用于当前用户及当前绑定账号，最多缓存 8 个准备好的客户端，180 秒自动关闭过期连接。发码携带 `preparation_token`，取出同一 SDK 客户端和 profile 后只调用 send-code；令牌只消费一次，过期或账号不匹配返回 409，不静默重复冷启动或自动重发。旧客户端未带令牌的请求保留原入口。

新检查涵盖归属校验、一次性消费、TTL 清理、容量限制、失败归还名额及同一 profile 的复用。只读实际准备实测首次约 10.7 秒，立即复用约 4 毫秒；这是准备接口的耗时，不是平台短信下发或手机送达耗时。前端浏览器检查使用 mock 发码，未发送真实短信。

## 工作台刷新加载

前端路由按页面延迟加载，避免账号页加载图表、图片工坊等全部代码。JS/CSS 在构建后生成 Brotli/gzip 文件；Caddy 从 `/srv/xhs-ui/assets/` 提供静态资源，带版本文件名使用 immutable 缓存，HTML 使用 no-cache。静态目录独立于用户主目录，发布前同步 assets 并校验可读权限；保留旧版本文件以支持已打开的页面。HTML 自带初始加载状态。

构建输出先进入隔离目录，检查后替换 frontend/dist 并同步 /srv/xhs-ui/assets/，更新 API 后仅重启 xhs-web，Caddy 配置变化仅重启 xhs-proxy。构建用独立 systemd unit，MemoryMax=768M、MemoryHigh=640M、MemorySwapMax=128M、CPUQuota=100%，不能并行运行其他大编译或重启 Hermes/Multica。实际浏览器结果保存在 `.local/sms-send-diagnosis/ui-check.json` 和 `cache-check.json`。

### 2026-10-15 页签切换优化

页面仍按路由懒加载，菜单悬停或获得焦点时提前下载对应代码，共用同一导入 Promise；预加载不挂载页面，也不请求业务 API。内容区独立 Suspense，下载页面代码期间导航栏保持可用。移动端点击同样提前加载。账号矩阵加入现有 KeepAlive 缓存，返回时先显示缓存内容，再后台请求最新账号数据；保留页上限为 6，私信等实时页面不额外缓存。

类型检查和限额构建通过。公开地址浏览器验证：悬停先请求目标代码、预加载不触发业务数据请求、加载期间导航可见、账号列表在重新验证请求未返回时仍可见；此次服务器浏览器测得缓存账号显示约 95 ms，无页面异常。证据 `.local/tab-speed/check.json`。前端已替换，旧版本备份 `.local/tab-speed/dist-before`，保留旧哈希资源；新静态文件及 br/gzip 放入 `/srv/xhs-ui/assets`，无需重启业务服务。

### 2026-10-15 验证码提交状态衔接

发码后的临时登录记录现在同时加密保存 PC 设备/session 签名状态、登录 b1、DSL 和按域 Cookie。提交验证码前恢复这些状态，避免新客户端将签名序号、profile 上报标记和网关 Cookie 重置。状态仅存在服务端加密字段，不随发码响应返回。旧会话继续兼容原 Cookie 格式；旧会话没有完整状态，验证本次修复需要重新发码。

回归使用合成数据，检查加密状态跨客户端恢复、签名序号连续、Cookie 域隔离、提交前恢复顺序及 API 保存/读取；不发送真实短信，不能代替平台实际登录验收。

### 验证码确认失败诊断

`internal/xhs/pc/phone/confirm` 现在持久记录确认结果：会话 ID、失败阶段
（checking_code/submitting_login/checking_identity）、HTTP 状态、平台返回码和凭据是否存在的布尔值。
不记录手机号、验证码、Cookie、mobile_token、session 或平台完整响应。
错误提示区分验证码校验未完成、校验通过但登录提交未完成，以及工作台请求头组装错误。
没有保留的历史响应不能倒推失败原因；发码被接受与验证码登录成功分别核验，
其他手机号登录成功不能代替目标手机号验收。真实验证码确认不自动重试。

回归包括三种 SDK 失败阶段、适配器错误传播、错误提示脱敏、失败日志持久化与身份读取失败，
以及既有状态恢复、预准备、重新登录和 Creator 登录检查；不发送短信或提交真实验证码。

### HTTP 471 安全验证响应

2026-10-15 登录提交诊断出现 HTTP 471，同时 JSON 含 success=true、code=0 且无 session。
不能仅凭 JSON 成功标志认为登录成功。PC 短信校验和登录提交先判断 HTTP 状态，471 明确归为
需要安全验证。会话进入 verification_required，拒绝重复提交验证码；账号中心确认转入 needs_user，
手机重复转发不再次确认。平台验证码/设备验证须由用户在官方网页完成，不自动重发短信或声称已恢复。
