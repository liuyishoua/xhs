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
