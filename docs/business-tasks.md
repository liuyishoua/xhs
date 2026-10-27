# 小红书工作台业务任务

总部只调用 `/api/business`，不使用伪造的用户 JWT。专用私有凭据只在本路由认可，固定 owner、PC account allowlist 和 operation allowlist；普通用户的真实 access token 也可使用本人的账号。私有配置 `.local/business-api.json` 含 `base_url`、`token`、`owner_id`、`account_ids`、`operations`，权限 0600；不要把 token 输出到报告。

## 接口

- `GET /api/business/capabilities`：账号资源、operation availability/input_schema、已有会话及上传素材 job 的 scope_options；附真实 Channel source/account identity。
- `PUT /api/business/goals/{goal_id}/authority`：`revision, enabled, expires_at, project_id, grants`。每个 grant 为 `account_id, operations, scope:{resource_ids, conversation_ids, include_goal_created?}, expires_at?`。每项grant可独立expires_at，到期只停止该项；enabled lease 最长65秒（总部推荐30秒），revision从0起，为非负整数（拒绝bool）；同revision仅允许续租；配置修改需新revision。project_id为服务器保存的goal绑定。
- `POST /api/business/tasks`：`request_id, goal_id, project_id, action_id, account_id, operation, input, authorization_revision, origin:'headquarters'`，返回202。完整规范化请求 fingerprint 对同principal/request_id不一致拒绝。先持久intent，再执行。
- `GET /api/business/tasks/{id}`：`state/status`兼容字段，结果来源与coverage。
- `POST /api/business/tasks/{id}/cancel`：仅 queued可取消；已在执行的写入不可撤销，不自动重复。
- `GET /api/business/events?after=cursor&goal_id=optional`：`items/cursor`（兼容 `events/next_cursor`），按principal过滤持久递增事件。

## 操作边界

|操作|输入与结果|
|---|---|
|profile.read|真实owned PC SDK self；验证external identity；公开字段|
|content.read|真实SDK最近笔记第一页，has_more，不冒充全部历史|
|public.search|keyword/page；真实SDK search_note，不使用演示搜索|
|conversation.read|当前账号本地缓存最近100会话|
|message.read|conversation_id必须授权；最近100缓存消息|
|material.read|publish_job_id必须scope授权；读取同owner同账号身份的既有素材job，返回asset ID/类型/上传状态，剥离文件路径与上传凭据|
|content.analyze|agent_id/text；工作台验证账号可用的content路线，走现有Channel→Multica。message_id=`business-`加request_id；总部事先把该消息精确绑定目标Project。兼容真实Channel succeeded及演示completed；structured status为needs_context/blocked/error或没有有效业务产物时任务failed。结果仅model_intermediate，不能作为真实发布/销售证据|
|draft.create|title/body，可给uploaded_asset_ids；只读当前账号身份、owner和resource_ids授权内现有上传图像素材。草稿本身不是发布。给图像素材时建立同owner同identity Creator job|
|content.publish|publish_job_id或draft_id；scope必须明确包含job，或include_goal_created允许同principal/goal/account持久provenance的新资源。仅未提交、即时、所有图像已上传验证的同身份Creator job。使用既有creator adapter；返回实际平台note ID，只证明提交，不证明公开可见/获客/成交|
|comment.reply|note_id/target_comment_id/text；note和comment精确scope授权，真实mentions验证own note/回复本人评论和来信身份，调用既有PC post_comment。只证明匹配提交ID，不冒充公开可见|
|dm.reply|conversation_id/message_id；只绑定已有真实入站消息且尚未提交模型，原listener必须enabled+auto_reply、state=listening且必要网络online、control不得要求人工批准。不修改监听开关。现有history→Channel模型→原WebSocket回复，发送claim额外检查goal authority；模型ready只是waiting_platform，必须matching mid/code=0/msg=ok平台ACK才succeeded|

`dm.initiate`与`video.publish`未实现，目录准确禁用。当前dm任务不能用来主动给陌生人发私信；停用的listener不会被业务任务打开。

## 持久性与安全

独立 `.local/business-tasks.sqlite3` 保存授权、task、完整request fingerprint、goal/project/action mapping、outbox和本goal/account新资源provenance；不改原监听/发布自动化开关。复核在提交、执行前和DM发送claim发生。每个平台写target另有冻结intent，换request_id不能重复提交同一发布job/评论目标。进程中断后读可恢复；写或模型请求结果不确定进入uncertain，不自动重发。发布未知结果同时将原PublishJob标记uncertain，原publish入口拒绝这个状态。

目标入站事件只针对明确接受任务所绑定的账号/会话且在任务创建后观察到的live inbound，排除历史导入；失效授权不再输出目标事件。同一goal/account/conversation/message持久去重。未绑定的普通消息不猜测归属。

授权撤回在写入已claim之前阻止操作；已claim并开始平台请求无法撤销，必须继续核对真实回执。平台送达与业务成交分别验收。

测试全部使用临时SQLite、fake平台/Channel适配器，没有真实发布、评论、私信。生产task worker随已有web service lifespan运行。新凭据配置不改变任何原控制开关。由总部统一部署，worker不自行重启服务。
