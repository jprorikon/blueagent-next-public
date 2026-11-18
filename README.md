# BlueAgent Next — Public Demo

实体优先的多角色 AI 运行时演示。项目展示如何从事件接入、路由、记忆披露、人格组装，到 Web / App 客户端交付，构建一条可恢复、可测试的完整链路。

> 本目录是专门整理的求职演示版。默认使用离线 Mock，不包含真实用户数据、访问令牌、内部服务地址、开发签名或第三方角色素材。

## 核心能力

- **无显式会话管理**：由 `(scope, entity, scene, purpose)` 推导线程并自动切片。
- **多实体与多身份**：记忆、关系、立场、声音、注意力均归属于 `CharacterEntity`。
- **记忆访问边界**：只有当前在场者属于记忆的 `audience` 时才可召回。
- **可替换模型层**：默认 Mock；也可连接 OpenAI 兼容端点。
- **持久化与恢复**：线程、事实、片段、活动、租约和 outbox 均支持落盘。
- **多端交付**：包含 Owner Web 调试台、独立 App Server、Unity 手机端与 Quest 3 MR 代码。
- **工程护栏**：包含分层、接口契约、权限、记忆隔离、流式交付和持久化测试。

## 快速启动

要求 Python 3.11+。核心 Mock 演示仅使用 Python 标准库。

```bash
cd blueagent-next-public
BLUEAGENT_BRAIN=mock python3 agent/run.py
```

首次启动会在本地生成 Owner token。按终端提示打开 Owner URL；token 只保存在本机运行目录，不应提交。

```text
http://127.0.0.1:8787/
```

## 离线测试

```bash
python3 agent/tests/run_all.py
```

部分语音测试需要可选依赖；核心演示不依赖外部模型或网络服务。

## 连接自己的模型

复制示例文件到仓库外或保持 `.env` 仅在本机，再显式指定：

```bash
cp .env.example .env
# 编辑 .env，填入自己的兼容端点与密钥
BLUEAGENT_ENV_FILE=.env python3 agent/run.py
```

不要把 `.env`、真实 token 或运行数据提交到仓库。

## Docker Compose

App Server 不会绕过账号初始化。先用本地 Agent 生成 Owner 账号，再为 App Server 生成虚构的本地账号与实体 token：

```bash
# 首次启动后按 Ctrl+C 停止；此时已生成本地 data/accounts.json
BLUEAGENT_BRAIN=mock python3 agent/run.py

python3 agent/tools/provision_app_server.py \
  --agent-accounts data/accounts.json \
  --app-accounts app-data/accounts.json \
  --secret-file app-secrets/agent.env

BLUEAGENT_BRAIN=mock docker compose up --build
```

`data/`、`app-data/`、`app-secrets/` 均被 Git 忽略。Compose 默认启动 Agent 与 App Server；网关仅在显式启用 `gateway` profile 时启动，不连接任何私人基础设施。

## 目录

```text
agent/
  brain/        领域模型、路由、记忆、人格与运行时
  adapter/      Discord、TRPG、App、语音等外部适配器
  server/       Agent HTTP / SSE 与管理接口
  client/       Owner Web 调试台
  persona/      原创虚构角色与共同人格规则
  tests/        Agent 离线验收
app/
  server/       独立账号、会话、公共区与媒体服务
  client/       Unity 客户端、共享 C# 逻辑与原创几何素材
  tests/        App Server 与客户端契约测试
mr/             Quest 3 专有工程
docs/           架构、接口、术语与生成文档
```

## 公开版边界

- 三位默认角色 **Mira、Aster、Nori** 及额外示例角色 **Lumi** 均为原创虚构角色。
- 立绘、头像、背景与表情包均为程序生成的抽象几何图形。
- 不包含第三方角色模型。3D 加载入口保留，未提供模型时回退到 2D。
- 不包含预编译原生库；移动端相关构建需自行准备对应依赖。
- Android 构建使用开发者本机签名，仓库不提供 keystore。
- 当前未指定项目级开源许可证；公开可读不代表授予再分发权。第三方组件仍按各自许可处理，见 `THIRD_PARTY_NOTICES.md`。

## 设计文档

- `DESIGN.md`：架构与安全边界摘要。
- `docs/APP-API-SSE.md`：App / Agent 接口契约。
- `docs/accounts-and-clients.md`：账号与客户端身份模型。
- `docs/FILE-TREE.md`：生成的文件树。
- `docs/ENVIRONMENT.md`：生成的环境变量表。
