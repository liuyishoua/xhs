# xhs 独立部署与 Bifrost 接入

本站直接部署作者成品，保留原有 React / Ant Design 界面、FastAPI 接口和注册登录流程。
上游仓库：<https://github.com/cv-cat/XHS_ALL_IN_ONE>；部署基线：`e86f82b`。

- 网站：<https://d.58nft.net:9450/>（首次访问跳转原版登录页，点击“注册”创建自己的工作台账号）
- 门户：<https://d.58nft.net/>，入口名为 `xhs · 小红书工作台`。
- 后端：`127.0.0.1:8772`，只监听回环地址。
- 后台服务：`xhs-web.service`；独立 HTTPS：`xhs-proxy.service`，均已配置开机启动。
- 数据库：`data/spider_xhs.db`；素材和导出：`backend/app/storage/`。
- 私有配置：`.local/runtime.yaml`，权限 600，包含本次生成的随机 JWT 签名密钥与 Fernet 加密密钥，不提交 Git。备份数据时同时备份该配置，以便恢复已加密的账号 Cookie 和模型密钥。

门户只提供直达链接，不共享本站账号或小红书登录状态。注册工作台后，在“账号矩阵”绑定小红书账号；AI 功能在“模型配置”选择 Multica 路由；私信监听按绑定 PC 账号启用。详细使用及验证范围见 [MODELS_AND_DM.md](MODELS_AND_DM.md)。

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci --no-audit --no-fund
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..
# 参考 deploy/runtime.example.yaml 创建 .local/runtime.yaml 并设置真实随机密钥。
CONFIG_FILE="$PWD/.local/runtime.yaml" .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8772
```

前端锁文件补齐了上游缺少的 `react-is` peer dependency，未修改产品界面或业务源码。
使用自定义 YAML 配置是因为当前上游 `get_settings()` 将 YAML 作为显式初始化参数传入，
同名字段不能按 README 声称的优先级由环境变量覆盖。`CONFIG_FILE` 本身有效。
后台调度已开启，沿用作者原有定时发布、自动任务、监控刷新及账号健康检查逻辑；
初始数据库为空，没有预设自动运营或发布任务。

```bash
sudo install -m 644 deploy/xhs-web.service deploy/xhs-proxy.service /etc/systemd/system/
sudo install -m 644 deploy/xhs.caddy /etc/caddy/xhs.caddy
sudo systemctl daemon-reload
sudo systemctl enable --now xhs-web.service xhs-proxy.service
# 后续应用更新，仅重启本站服务
sudo python3 deploy/publish_frontend.py /absolute/validated/frontend-build
sudo systemctl restart xhs-web.service
journalctl -u xhs-web.service -n 50 --no-pager
```

## 本次验证

2026-10-10：前端 TypeScript 与 Vite 生产构建成功；作者 130 项后端测试通过。
测试使用临时配置、独立数据库与原测试的接口替身，没有操作真实小红书账号。

公网 TLS、健康接口、原版登录页及静态资源通过检查；浏览器验证桌面与移动登录页，
另用临时数据库验证注册、平台选择、首页及 13 个功能页面，未出现 JavaScript 异常或服务端 5xx。
Bifrost 使用临时账号与数据库验证入口渲染和新窗口跳转，不读取生产账号或会话。
未验证真实小红书登录、抓取、AI 调用或发布；这些能力需要连接相应账号与模型后使用。

先前 xhs-spider 目录中的自建工作台草稿未部署，也未加入门户。

## 工作台用户与社交矩阵隔离（2026-10-15）

使用现有工作台登录注册。每个工作台用户可绑定多个小红书 PC/Creator 账号，
只能读取、管理和操作自己绑定的账号；注册新用户不继承已有用户的社交矩阵。
账号列表、私信和历史、评论监听、主动探索配置与记录、任务及素材分别检查用户归属。
替换请求中的账号或资源 ID 返回 404；普通业务接口未登录返回 401。
公共平台目录和健康检查仅提供产品元数据，维护控制与短信入站继续使用专用鉴权。

素材文件需要本人访问。浏览器图片使用 HttpOnly、SameSite=Strict 的短期 Cookie，
仅限素材读取路径；HTTPS 下标记 Secure，不能凭该 Cookie 操作账号或发送消息。
所有 API 响应禁止缓存。退出登录使该用户现有访问和刷新凭据失效，清空本地页面状态；
切换用户、跨标签页登录变化及旧请求返回均不会复用上一用户的界面数据。
浏览器记住的聊天、评论和 AI 执行账号也按工作台用户分开保存。

迁移 `c8f419d50e32` 增加登录凭据版本，并使 SQLite 社交账号 ID 不再因删除而复用。
历史账号所有者不变；迁移前备份数据库并在副本比对账号、加密登录材料和聊天记录。
验证使用独立注册用户、模拟社交账号和临时数据库，没有对真实社交账号发送消息。
隔离回归：`python -m pytest tests/backend/test_workspace_isolation.py -q`。


## 统一运营入口与平台端口

运营应用中心：`https://d.58nft.net:9461/`，源码、独立用户库、密钥、前端产物与部署配置均归相邻的 `ops-hub` 项目维护。小红书继续使用 `https://d.58nft.net:9450/`；旧应用中心入口跳转到新地址。本项目的发布脚本仅发布小红书，不影响中心。

生产通过 `OPS_IDENTITY_URL=http://127.0.0.1:8787` 校验业务访问身份。`/api/auth/*` 兼容代理转到中心；小红书不再注册、校验或撤销本地密码。未配置中心时保留独立开发模式，生产不得删除该配置作为故障回退。中心不可用时认证返回 503，不能放宽权限。

现有用户 ID 与业务外键保持不变。新中心用户首次进入小红书时，仅建立该 ID 的本地业务映射，不继承任何现有子账号或资料。本地旧密码哈希保留为迁移历史，生产不使用。业务数据隔离继续由各资源的用户归属校验执行。


## 多标签页登录回归

同一运营账号刷新令牌不代表切换账号。收到其他标签页的存储事件或重新获得焦点时，只校验统一身份且合并并发请求；用户改变才重新加载当前路由，退出或会话失效才返回登录。初次认证的网络故障显示可重试提示，不能绕回中心形成循环。身份交换超时为 10 秒。

浏览器回归脚本 `tests/browser/multitab_identity.py` 需要安装 Playwright，业务 API 全部使用模拟数据。可传入构建目录校验发布前资源；不传参数则校验部署后的资源。覆盖双私信标签、同账号令牌轮换、账号切换、退出、认证故障及原路由重试恢复。
