# Multica 模型路由与小红书私信

模型配置入口：<https://d.58nft.net:9450/models>。
私信监听入口：<https://d.58nft.net:9450/platforms/xhs/messages>。

家庭网络固定出口、连接检测和部署说明见 [HOME_NETWORK.md](HOME_NETWORK.md)。线上 PC 登录、笔记读取和私信统一通过 Mac mini 家庭出口；断线暂停处理，不自动切换到服务器直连。健康检测与每条消息的手机送达确认分别记录。

模型地址、Key、运行时在 Multica 管理。工作台后端沿用 `.local/web/channel.json` 的来源凭据，读取 Channel 的来源范围目录。每位工作台用户分别为内容、图片、私信用途保存默认路由，底层 agent/model 由 Channel 路由指向 Multica。原模型记录保留；保存新的用途配置后，新任务切换到 Multica，浏览器不取得来源凭据。设置菜单及原设置内容已移除，旧 `/settings` 跳转模型页。

## 使用

1. 在模型配置选择内容、图片、私信路由，点击保存。
2. 账号矩阵绑定有效的小红书 PC 账号。
3. 在私信监听选择该账号和路由，选择仅生成回复或自动发送，再点击开始监听。
4. 更换默认私信路由不会重写已运行监听；在监听页重新应用配置。

图片分析通过 Channel 上传图片附件；参考图片先上传素材库，不由服务端抓取外部链接。图片生成要求 Multica agent 具备相应能力并返回可访问的 HTTPS 图片地址，仅配置文本 agent 不代表已具备图片生成能力。

## 私信生命周期

历史上下文、三档控制和最新验证见 [DM_HISTORY_AND_CONTROL.md](DM_HISTORY_AND_CONTROL.md)。下文基础状态说明以该文档补充为准。

`history_pending → queued → waiting → ready → sending → sent`；请求批准模式在 ready 前增加 approval_pending。以绑定账号和稳定消息 ID 去重；Channel 提交载荷在网络调用前持久化，超时后使用原 ID、原内容重试。新文字私信使用原用户 ID 作为 conversation_id，附上 `surface=dm`、`event_type=dm.received` 和 `reply_draft` 类别。

监听仅处理启用后的单人文字私信；自己的消息、其他接收账号、群聊和非文字消息不触发模型。WebSocket 断开自动退避重连，并补收最近两天内断线期间、启用时间以后且晚于会话最后接收消息的新文字，以稳定消息 ID 去重；启用前历史不触发回复。每个新消息仍读取该用户最近 48 小时双向历史作为上下文。私信协议隔离在 `im_protocol.py`，来自本机较新 xhs-spider 的已捕获实现；认证沿用本工程签名器，校验 Cookie 对应用户 ID，取得 RWP token 后连接。协议变化、过期 Cookie 和网络异常会显示连接状态，不模拟成功。

模型须返回 `{"reply_text":"回复正文","requires_human":false}` 才进入可发送状态。特殊事项在帮我批准模式进入 held；完全访问模式不等待人工，缺少合法结果不发送。缺少约定格式的结果进入 failed；Channel 的模型任务执行失败进入 failed。普通内容任务提取 Channel `structured.reply` 作为结果，保持原标题、正文、标签等入口格式。

代聊 Agent 启动时先通过 `multica issue get <当前任务 ID> --output json` 读取本轮任务，聊天快照位于任务正文中；评论触发时只读取得本轮触发评论。这些内部任务读取属于已授权的输入获取，不属于禁止的对外社交平台操作，也不违反“不查阅旧任务重建历史”的限制。身份指令必须在读取本轮快照前说明这个边界，否则模型可能在看到聊天之前误拒绝任务。完全访问模式返回 handoff/requires_human=true 属于输出约定失败，记录为 failed，不能显示为正常“不回复”。正常 no_reply 仍由聊天内容决定。

发送前原子记录 sending 和稳定 mid；只有同 mid 的小红书 `chatACK.code=0` 且 `msg=ok` 才记录 sent，页面显示“平台已接收”。成功回执和发送账号的云端历史不能证明对方手机已显示或已读；人工确认的记录单独显示“已人工核对发送”。匹配的成功、拒绝和不完整回执均保留 code、msg、messageid、storeid、ts，不保存 token；旧记录没有保存 msg，无法事后补推原始说明。缺失回执、断线或服务重启留下的 sending 进入 uncertain，不自动重发，并阻止该用户的后续发送。用户核对真实小红书记录后，可确认已发送或跳过，系统不以核对操作重新发送。每个账号的其他用户会话可继续处理。

发送者昵称通过已校验身份的 user/me 读取，与官方网页构造一致。昵称对齐只修复已确认的协议差异，不代表已经证实手机不可见问题的原因或修复；真实送达须核查接收侧。

关闭自动发送时保留 ready 回复；重新开启会处理这些待发送回复。停止监听不会删除历史记录，重新开启采用新的历史截止时间。删除账号前需要先停止监听并等待在途发送结束。

部署使用单个 uvicorn 进程，由应用 lifespan 启动监听 supervisor；不要为该服务启动多个 uvicorn worker。数据库升级新增私信状态、历史和会话分析表，由 Alembic 自动迁移。没有启用的监听时不连接任何小红书账号。

## 验证

2026-10-10：XHS 后端完整回归 158 项通过，后续新增结果解包等定向测试也通过；Channel 全量回归 98 项通过。前端 TypeScript/Vite 构建通过。独立临时数据库的浏览器验证覆盖真实路由目录、三种用途保存、设置入口删除、消息记录和桌面/移动布局。

合成消息完成真实 Channel → Multica 调用，返回 reply_text 且 requires_human=false；最终执行记录保存在 `.local/model-dm-verification.json`，两次合成调用的 issue/task ID 保存在 `.local/model-dm-verification-attempts.json`。完整私信收取、模型结果消费、发送及匹配回执使用合成平台替身验证，没有向真实小红书用户发送测试消息。真实小红书收发仍需有效 PC 账号联调。
