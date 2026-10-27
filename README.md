# xhs

> 文档中的 example.com 域名、192.0.2.* 地址、/opt/operator 路径及 20000 段端口均为示例，请按实际部署配置替换。私有账号配置与运行数据不随源码提交。

[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)

独立运行的小红书账号与内容工作台，基于 [cv-cat/XHS_ALL_IN_ONE](https://github.com/cv-cat/XHS_ALL_IN_ONE)。
包含账号连接、笔记发现、内容库、AI 改写、图文发布、私信会话以及可选 Channel 模型协作。
FastAPI / SQLAlchemy 后端、React / Vite 前端、SQLite 数据库。

## 本机安装

先安装 **Python 3.10+、Node.js 20+**：

```bash
git clone https://github.com/liuyishoua/xhs.git
cd xhs
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install
cd frontend
npm ci
npm run build
cd ..
```

复制 `config/default.yaml` 为私有 `.local/runtime.yaml`，生成自己的随机密钥，
设置 `frontend.serve_static: true`，保留 `scheduler.enabled: false`，然后运行：

```bash
CONFIG_FILE=.local/runtime.yaml .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 20004
```

打开 **http://127.0.0.1:20004**，按页面完成工作台注册与平台账号连接。
配置加载、数据库迁移、HTTPS 及平台网络要求见 [部署说明](deploy/README.md)。
本项目没有通用离线演示启动参数；后端回归测试使用临时数据库和模拟平台。

## 真实账号与模型

工作台账号、小红书 PC 账号和 Creator 账号分别管理。扫码或手机号登录按平台当前要求完成，
Cookie 与平台会话加密保存到本地。发布、发送和自动化依赖自己的账号权限及明确配置。
Channel 和 Multica 集成是可选能力，接入说明见 模型与私信部署（本地资料）。
需要家庭网络时配置自己的代理和授权，不直接复制历史服务器或 Mac 主机地址。
服务恢复不代表平台登录或真实业务一定成功，应检查实际状态及回执。

## 数据与备份

- `data/`：业务数据库；`backend/app/storage/`：素材、导出与业务文件。
- `.local/`：私有部署配置、模型路由和运行状态；账号密钥必须与数据配套备份。
- 备份前停止相关服务，复制数据目录、素材和密钥，不能只备份数据库而丢失加密密钥。
- Cookie、消息、数据库、日志、`.env` 和虚拟环境均不提交 Git。

## 验证

```bash
.venv/bin/python -m pytest tests/backend -q
cd frontend
npm run build
```

自动化回归不使用真实平台发布作为验收。原作者界面和 SDK 说明见
[docs/upstream-readme.md](docs/upstream-readme.md)，其中历史截图不代表本地修改版界面。

## 目录

- `backend/`：API、模型、平台适配、私信和业务服务。
- `frontend/`：React 页面；`apis/`、`xhs_utils/`：原平台接口与签名代码。
- `deploy/`：服务和集成示例；`tests/`：隔离回归测试。

## 使用许可

**本项目仅供学习、研究、测试及其他非商业用途，不得用于商业目的。**
对本仓库中版权所有者拥有权利的代码，采用
[PolyForm Noncommercial License 1.0.0](LICENSE)；商业使用须事先取得版权所有者的单独授权。
本项目属于**源码可见、限制商用**，不应标为 MIT 或 OSI 意义上的开源许可证。

第三方代码、素材和依赖仍受其各自许可条件约束，来源与许可范围见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本声明不追溯改变第三方代码或此前已发布版本的许可。
