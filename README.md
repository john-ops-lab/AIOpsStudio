# AIOpsStudio 运维工作台

以 Chat 为中心的只读运维分析工作台，将 Skill、MCP、模型配置、分析报告与原始证据放在同一工作空间。采用 [MIT 协议](LICENSE)。

[完整设计](docs/design/AIOpsStudio完整设计.md) · [当前规格](openspec/specs/) · [设计索引](docs/design/README.md) · [开发工作包](docs/design/AIOpsStudio完整设计.md#15-开发阶段依赖与工作包) · [验证摘要](docs/validation/2026-10-22.md) · [交付状态](docs/status.md)

当前应用、数据库、执行器与测试面向 Mac 本机运行。此仓库提供通用部署入口；数据源、身份映射、查询目录和模型由部署者自行配置。公开文档是去标识化说明，图片是完全新制的通用演示素材，均不对应某个现场环境。

## 功能

- Chat 工作台：服务端会话历史、固定输入框、可折叠分析画布；选择 Skill、目标和固定时间窗，查看分析步骤、报告与原始证据。继续追问创建关联轮次，保留旧报告，刷新不重复执行。
- 用户与附件：独立账号、Cookie 会话、admin/operator 角色；消息、轮次、证据与附件按当前用户隔离。管理员管理共享配置，不能默认读取他人私有历史。UTF-8 文本有界作为用户材料；二进制文件可保存下载，未实现的内容提取明确标记。
- Skill 管理：草稿编辑、参考文件、输入 schema、工具依赖、校验、不可变发布、版本查看、回退、停用/立即阻止与草稿试运行。
- MCP 管理：连接、认证、CA、超时、目录发现、schema、只读白名单和参数 JSON 试调；固定工具别名，保留错误与原响应。
- 模型与运行：OpenAI 兼容接口、版本化模型设置、中文工具往返测试；调用来源、耗时、已知用量、未知项、队列和配置审计。后续模型验证优先 MiniMax，仓库不预置模型端点或凭据。

## 安装与启动

需要 Python 3.12、uv、Node 24 和 npm；使用内置数据库启动功能时还需要可用的 Docker。数据库、容器和状态目录应为此应用独立配置。

```sh
git clone https://github.com/john-ops-lab/AIOpsStudio.git
cd AIOpsStudio/backend
uv sync --frozen --python 3.12
cd ../web
npm ci --legacy-peer-deps --ignore-scripts
npm run build
cd ..
python3 deploy/local.py start
```

默认仅绑定回环地址。Web 为 `http://127.0.0.1:3000`，Python API 为 `127.0.0.1:8000`，PostgreSQL 为 `127.0.0.1:5432`。数据库默认容器 `aiopsstudio-db`、卷 `aiopsstudio-db-data`、用户和库名 `aiopsstudio`，镜像 `postgres:16-alpine`。未指定 Docker context 时使用当前 context，不切换全局设置。管理器创建的数据库容器和卷带有状态目录归属标签；若同名资产属于其他实例，会拒绝启动或挂载。

默认状态目录为 `~/.local/share/aiopsstudio`，可通过绝对路径的 `AIOPSSTUDIO_STATE_DIR` 指定。首次启动生成独立管理员口令和主密钥；已有文件不覆盖。初始账号 `admin`，口令文件位于状态目录的 `private/admin-password`。管理员在页面创建普通用户，凭据通过管理表单只写提交，已有密钥不回显。

仅用新环境变量指定自定义状态目录时，必须同时提供 `AIOPSSTUDIO_DATABASE_URL`，或在部署 JSON 中显式填写 `database.container`、`volume`、`user`、`name` 四项；空目录首次启动与后续启动遵循同一规则，不能仅凭目录存在与否猜数据库。旧 `STATE_DIR` 与同值新键并存时保留旧资产选择，冲突值拒绝。

`AIOPSSTUDIO_DEPLOY_CONFIG` 可指向仓库外的绝对路径 JSON。以下为通用默认配置，可按本机需要调整：

```json
{
  "ports": {"web": 3000, "api": 8000, "postgres": 5432},
  "database": {
    "container": "aiopsstudio-db",
    "volume": "aiopsstudio-db-data",
    "user": "aiopsstudio",
    "name": "aiopsstudio",
    "image": "postgres:16-alpine"
  },
  "node_binary": "node",
  "ssh_tunnel": {"enabled": false}
}
```

可选字段 `docker_context` 指定数据库所在 context。若使用已有 PostgreSQL，设置仓库外的 `AIOPSSTUDIO_DATABASE_URL`；启动管理器不创建 Docker 数据库，仍管理本应用独立口令和主密钥。正式工作台拒绝 SQLite，单测可使用隔离 SQLite。

### 已有安装的更名边界

当前产品、仓库、Python 包及环境变量主前缀统一使用 AIOpsStudio / `aiopsstudio` / `AIOPSSTUDIO_`。原环境变量前缀仍由服务端集中兼容；新旧同项同时设置且值不同会拒绝启动，避免连接到错误实例。Node 与 Vite 共用 `AIOPSSTUDIO_API_URL` 解析；浏览器不读取凭据或旧配置。

更名不自动移动状态目录、重建数据库、轮换主密钥或覆盖已有配置。未显式指定状态目录时，服务端只读检查当前及旧版默认目录：仅存在一个时复用该目录，多个并存则拒绝启动，要求通过 `AIOPSSTUDIO_STATE_DIR` 明确选择，不自动合并。已有安装的端口、数据库连接及其他部署参数仍须在仓库外按实际配置提供，并保留匹配的主密钥；不应把全新默认目录当作已有实例迁移完成。

已有数据库完整使用旧版三张工作台表时继续复用，新安装使用新表名；新旧表混用或任一套不完整时拒绝初始化。历史数据库容器/卷只有与状态目录匹配的归属标签才由管理器接管，缺少标签时使用显式外部数据库连接，不冒认资产。有效旧 Cookie 升级为当前 Cookie，保留原会话剩余有效期；新旧 Cookie 值冲突时返回 401 并清除两者。并行独立实例的目录、数据库、卷与端口需保持独立；隔离测试不能代替已有实例升级验收。

SSH 转发默认关闭。仅在接入确有需要时设置 `ssh_tunnel.enabled=true`，同时提供 `host`、`local_port`、`remote_port`；`remote_host` 可选，默认回环地址。所有地址与端口由部署者提供，仓库没有内置源主机或隧道。

```sh
python3 deploy/local.py status
python3 deploy/local.py stop
python3 deploy/local.py start
```

`stop` 停止本应用记录的进程，保留数据库和卷。`start` 保留已有配置与分析历史；进程不会在 Mac 注销或重启后自动恢复。独立 `init`、`db`、`tunnel` 命令可用于按阶段准备，不会隐式导入源配置。

## 数据源与 Skill 引导

可以直接通过管理页面配置；批量引导必须显式提供仓库外、权限为 `0600`、`version` 为 `1` 的 JSON：

```sh
backend/.venv/bin/python deploy/seed.py --bootstrap /absolute/external/bootstrap.json
```

可选 `--url` 指向本机工作台，`--username` 选择管理员，`--password-file` 指向仓库外口令文件。引导配置支持以下字段，实际值均由部署者提供：

| 字段 | 内容 |
| --- | --- |
| `mcp_servers` | 连接名称、URL、认证、可选 CA 和凭据，以及确切工具名组成的 `allowed_tools` |
| `model_configs` | 名称、`provider=openai_compatible`、模型名、`base_url` 和可选凭据 |
| `default_model_name` | 可选的默认模型配置名称 |
| `settings` | 预算、Ongrid 接入、`query_catalog` 与 `identity_mapping` |
| `skills` | 名称、正文或相对引导文件的 `content_file`、schema、参考文件与工具依赖 |

MCP 依赖以 `server_name` 和 `tool_name` 指明；Ongrid 依赖使用 `provider=ongrid` 与工具名。引导 JSON 拒绝重复键和非标准数值；`content_file` 必须位于引导文件所在目录内。引导会实际发现 MCP 目录，按显式只读白名单校验再发布；不会执行模型或业务工具。已发布的同名配置保留并计入 `preserved_existing`，同名未发布草稿需在管理页处理。没有默认源地址、模型、白名单或查询目录；`skills/` 仅提供可审查的通用文本，需按实际来源明确依赖后发布。

## 执行与证据边界

分析、Skill 试运行、工具试调和模型测试共用数据库控制的单执行槽，等待队列最多 10 项。普通用户仅执行已发布且启用的 Skill；配置变更和试调由管理员操作。消息先按用户和会话幂等持久化，再在模型调用边界消费。改变主目标、Skill、主窗口或终态后补查会创建新 attempt。

会话关联轮次保留历史，不受旧独立任务七天清理影响。上传单件上限 5 MiB、每用户 100 MiB、请求体上限 8 MiB；UTF-8 材料单件至多 12000 字符、单次合计 24000 字符。空间不足时拒绝新增；截断和未提取状态明确保留，旧证据不自动作为新轮依据。

工具范围取所选 Skill 依赖、明确通用方法、平台白名单与源身份权限的交集。发布内容、目录、模型、输入和预算在提交时冻结；普通停用阻止新任务，立即阻止和凭据撤销拦截后续派发。源身份是否只读和具备何种范围，需由部署者验证，应用白名单不能代替源端权限。

工具原响应先保存，再提供带 `evidence_id` 的有界阅读视图；分页、截断和未知项保留。分析最多 4 个探索模型回合，并为禁工具报告回合预留预算。报告失败时明确展示平台模板，不能冒充模型分析。默认新任务累计 token 预算为 100000，旧 attempt 不变。`source_request_count` 记录应用发出的源 HTTP 请求，`mcp_request_count` 记录 MCP 逻辑发现/调用；MCP 内部请求次数未知时不补零。

HTTP 成功、工具成功、模型报告、业务覆盖与用户接受分别记录。当前登记拓扑不能重建故障时拓扑，源告警筛选和时间线可能存在覆盖限制。当前未提供 GLPI、自动告警触发、自动修复、Shell 执行、SSO 或生产高可用；完整断电恢复和 Webhook 联合场景仍待验证。

## M1 可信分析增量

本分支已接入固定对象/窗口、覆盖状态、证据与 Skill 资料按需补读、结构字段核对，以及每用户源授权和模型实际 HTTP 出站检查。没有配置身份/查询契约时先补信息，没有 source/egress 策略时默认拒绝。管理员可在模型设置页维护查询目录与两类策略；普通用户只看到可选配置元数据。

[完整设计](docs/design/AIOpsStudio完整设计.md)、[M1 合同](CONTRACT.md#当前增量m1-可信分析与数据边界)、[本轮验证](docs/validation/2026-10-23-m1.md)区分开发分支、合成回放、真实 MiniMax 样本和业务接受。现有运行实例未自动升级，正式发布质量门槛及后续协作能力继续按路线图开发。

## 验证与开发

开始开发前安装当前副本的[提交与推送安全门禁](security/README.md)：

```sh
brew install gitleaks
python3 scripts/install_security_hooks.py
```

需要 Gitleaks 8.30.1 或兼容的 8.x 版本。安装后，`git commit` 自动检查实际暂存树，`git push` 自动检查各目标分支/标签的全部历史；命中密钥、本机配置、现场地址、未复核图片或检查失败都会阻断。新 clone 需重新安装钩子。仓库外私有 profile 可补充实际凭据和现场标识的只读比对；CI 执行通用规则复核。独立全历史检查：

```sh
python3 scripts/security_scan.py history
python3 -m unittest discover -s tests -p test_publication_security.py -v
```


```sh
cd backend
uv run pytest -q
cd ..
node tests/server-safety.mjs
cd web
npm run build
cd ..
openspec validate --all --strict
```

单测中的 fake 模型和本机 fake MCP 只证明协议及错误路径。已启动、已配置的 PostgreSQL 实例可在 `backend/` 执行 `uv run python ../tests/chat_acceptance.py` 检查隔离与持久化；默认不调用模型。`--live` 需要显式 `--target` 或 `--ci-id` 和真实配置。其他源接口验收参见 `tests/live_acceptance.py --help`，由部署者明确提供目标，不能把连通性当业务验收。

[历史公开摘要](docs/validation/2026-10-22.md)记录 84 项后端测试、16 项 HTTP 检查和 19 项 Chat 联核的历史结果，以及首次失败和 `partial` 边界；它不是当前提交、任意新环境或新模型的验收。原始收据、对话、附件、证据与日志不在仓库内。公开摘要不保留运行 ID、收据哈希或原环境映射。

后续开发遵循 [AGENTS.md](AGENTS.md) 与 OpenSpec；当前行为在 `openspec/specs/`，变更在 `openspec/changes/`，勾选任务不能替代验收。公共历史清理见 [sanitize-public-history](openspec/changes/sanitize-public-history/proposal.md) 与[验证及残留状态](openspec/changes/sanitize-public-history/verification.md)。

## 备份与目录

备份本应用 PostgreSQL、Skill/证据/附件目录及独立主密钥恢复材料。恢复时停止本应用写入并保持数据一致；仅恢复数据库不能恢复附件字节或缺失的主密钥。

`backend/aiopsstudio/` 提供配置、认证、Vault、存储和执行；`web/src/` 为 React 界面，`web/server.mjs` 提供同源代理与持久 Runner；`deploy/` 提供本机生命周期和显式引导；`tests/` 提供验收入口。公开 [Chat 演示图](docs/design/assets/20261022-AIOpsStudio-Chat工作台Demo-v0.1.png)及架构图均为完全新制的通用演示素材，不是实际验收截图。第三方依赖保留各自许可，不分发安装目录。
