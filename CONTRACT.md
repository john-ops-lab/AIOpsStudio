# AIOpsStudio 实现协作合同

## 当前增量：M2 集成工作台（开发分支）

R05/#6、R06/#7、R08/#9、R09/#10 使用一个集成 PR、四份 OpenSpec 和分项验收，基线 `fa91b92`。本节描述本分支已经落盘的接口机制；未代表 M2 已合并、部署或通过真实业务/用户接受，原 Mac 配置、历史和附件不在本轮开发/故障验证写入范围。总边界见 [M2 实施契约](docs/design/M2集成实施契约.md)和[最新完整设计](docs/design/AIOpsStudio完整设计.md#25-m2-集成范围与后续边界)。

- 新发布默认 formal 且要求 revision CAS；需要当前实现/suite/scorer 绑定的受控 R01 流水线证明，以及候选实际运行/必要基线运行的服务端合同评分。POST `/{skills,mcp-servers,model-configs}/{id}/quality` 只接受运行引用，GET 返回服务端评价；GET/POST `/quality/pipeline` 由管理员查看/执行固定回放，POST 仅接受 `{}`，不接受路径、分数或 pass。实验版本不切换 current，历史正式版本继续保留；未知/未执行不算质量通过。具体适用范围见四份 change。
- 队列入队与概览统一使用 settings.queue_limit，降低上限只阻止超限新增，已排队任务保留。Skill 文本包校验后原子激活，references 有固定版本/路径/摘要；MCP 目录完整性、schema/定义/依赖变化与凭据在发布/回退时复核。源权限与出站始终沿用 M1 frozen/latest 约束。
- POST `/conversations/{id}/messages` 追加顶层 `{mode?:analyze|explain|followup,parent_run_id?}`。省略兼容 analyze；explain/followup 选择同会话终态父轮且总是新建预算轮。explain 禁来源发现和工具、拒绝新附件，只解释仍获授权的保存结果；followup 可补查。普通归档/已删除会话禁止新分析；显式 legacy_archive 保持原只读契约。
- GET `/conversations?search=&archived=false&status=&start=&end=&limit=&cursor=` 返回 `{items,next_cursor}`，limit 为1–100；搜索/owner/状态/时间/归档/删除由 SQL 过滤，稳定 created_at/id 游标绑定筛选范围及首批上界。POST `/{id}/archive` `{archived,expected_revision}`；GET `/{id}/export` 返回有界 JSON 与授权附件字节。显式旧档案可查看/下载/导出，修改返回409 `legacy_archive_readonly`。
- 会话和附件 POST `/{id}/deletion-preview` 返回 `{preview_token,expires_at,counts,blocked,reasons,notice}`，POST `/{id}/delete` `{preview_token}` 确认；令牌绑定 owner/revision/引用并在确认时复核。运行中或保留报告引用阻止删除；先提交最小 tombstone，再清当前对象文件/精确检查点，失败可按原令牌幂等重试。已删除材料的详情/旧下载入口拒绝。
- GET `/attachments/usage` 返回元数据/实际/孤立字节和缺件对账；POST `/attachments/orphans/deletion-preview` 与 `/orphans/delete` 仅处理当前 owner 固定命名且无元数据的文件，写入与回收共用事务。普通附件5MiB/用户100MiB及部署全局配额继续适用。
- GET `/runs/{id}/usage` 必须先用 `runtime.require_owned` 取已授权私有运行，再仅输出用量投影；已知累计量与未知 token 分开，无完整用量/版本化价格返回 `estimate:null`，缓存只计一次，供应商内部重试保持未知。POST `/runs/{id}/repair-report` 只重存原报告附件并修复关联，不调模型/来源，返回 `{delivery_status,report_attachment,error}`。
- Store 的执行 scope 绑定 run/owner/generation/到期时间，数据库/事件在事务入口与 COMMIT 前 fencing，新文件以临时文件发布并登记回滚；PG checkpointer 共用 advisory lock 和代次验证。旧 worker 失效后拒绝后写；中断明确终结并保留用量，显式重试新建关联轮，不自动重放 running 图。
- 离线备份固定应用表/PG 检查点、制品字节摘要与版本引用，主密钥独立提供；拒绝活跃任务、缺件/摘要/密钥错误及非空或源目标重叠的恢复。恢复丢弃旧 session/租约，设置 `restore_guard`。未核对备份外最新授权/撤销事实前，除 login/session/logout/只含核对标记的 `/restore-status`，工作空间 API 全部503 `restore_unverified`，保留期清理暂停。

四份 change：`complete-management-acceptance`、`improve-long-running-chat`、`manage-history-attachment-lifecycle`、`verify-execution-recovery`。M2 不包含工单接入、变更分析、Hypothesis/Verdict、告警 Webhook/Case/共享、完整断电或高可用验收；这些按 R10–R13 或候选项另交付。下方保留 M1 与首版合同，M2 同名行为以本节为准。

## 当前增量：M1 可信分析与数据边界

- 正式源工具成功和错误返回先按既有 `source_evidence` 结构投影为单条 ledger entry，补读使用 `reading` 规则；原始证据保持，禁止用通用协议文本绕过来源字段权限。
- 当前 attempt 的助手工具请求与对应返回按完整组保留，最多两组、48 KiB；不从历史会话恢复协议。摘要账本继续按当前权限投影，策略变化停止重放旧消息。`model_protocol` 必须显式准入兼容供应商的 `reasoning_content` 字段；推理只用于当前尝试的内存协议，不进入页面、报告、事件或检查点。不自动改写已有策略。
- 模型请求以当前剩余 wall 预算为界、单次最多180秒；探索保留原报告时间，报告不能增加总预算/回合。已派发而中断、没有取得完整用量的调用标为未知。
- 旧历史档案只允许原 owner 查看、下载；执行/重试/补读/模型继承拒绝。离线清单绑定原内容，不能自动扩容；原配置和原记录保持。

当前已按 R01–R04、R07 的五个 OpenSpec change 实现并合并；审查升级和真实模型复验见 `docs/validation/2026-10-23-pr15-review-upgrade.md`，初次记录保留在 `docs/validation/2026-10-23-m1.md`。此节覆盖下方历史合同的同名行为；代码、合成通过、真实模型样本、业务接受和运行实例分别记录。

- `analysis_context` 固定完整 canonical 五维对象、来源映射有效期、fault/baseline/change/validation 窗口、时钟偏差及版本化查询目录。身份未定先 `needs_input`，模型/源调用为零；服务端绑定身份与绝对时间，模型不能覆盖。`result_identity` 单独核对返回行/序列对象及全部范围维度；请求范围不冒充返回对象。无历史时间绑定的拓扑不视为故障时拓扑。
- `PolicyService` 读取 `access_policy/main`、`egress_policy/main`。没有策略默认拒绝；frozen 与 latest 取约束交集，管理员无隐含源数据权限。每个连接实际 HTTP 派发及模型实际 SDK 请求复核；MCP 目录用 `$catalog` / `discovery`，告警列表/详情浏览用 `browse`，均需显式授权。实际目标精确绑定且拒绝重定向。
- GET/PUT `/access-policies/source|egress` 仅管理员；GET 返回 `{configured,revision,policy?}`；PUT `{expected_revision,policy}`，策略 version 内容不可改写。普通用户的 Skill/模型仅返回已发布可用选择元数据；MCP 仅在有目录权限时返回选择元数据；settings 仅可选默认模型，versions/正文/连接配置不外泄。
- 历史报告、继承历史、会话标题/消息、调用、片段、生成报告下载和订阅重新核对最新来源权限；未知来源旧派生材料拒绝。用户自传文件保持用户材料语义，不自动升级为源证据。
- POST `/runs/{id}/material-reads` `{resource_type,selector,evidence_id?}`，或 `skill_reference` 加 `skill_id,version_id,path`。selector 支持 JSON Pointer、offset/limit、fields、series、绝对时间；不访问任意路径/URL。GET `/fragments/{anchor_id}` 重新授权、校验保存 SHA、重读原选择器并核对原数组坐标/投影字段。
- 补读回执包含 `data,position,selector,anchor_id,material,saved_root,completeness,usage`。`position.source_indices/source_total_items/projection_fields` 区分筛选后的视图与原始位置。模型每次最多16KiB、16次/128KiB；人工16KiB、512次/8MiB，完整返回包计 UTF-8 字节，估算 token 不冒充 provider 用量；失败/重复均保留明确边界。
- Skill 主体正文固定加载，reference 只提供固定版本清单，使用 `read_saved_material` 按需取内容；成功补读进入下一轮有界上下文。模型补读参数错误作为可纠正的安全工具错误反馈，策略拒绝仍停止，不绕过预算。
- 报告保存 `grounding`、稳定 fact/claim 与 report_version。数值、单位、范围、对象、片段及冻结评估规则由程序核对；`verified` 只表示结构字段核对，模型措辞独立标未核验。拓扑潜在影响、观测异常、反证、未知、候选分开；不确认根因/恢复。
- 出站类型逐字段投影；未知字段/类型、已知撤销前后凭据、未绑定参数均拒绝。会话别名映射仅服务端；实际 HTTP 验证组合提示对应最新策略 digest，不能以旧批准 hash 绕过收紧。HTTP transport 错误只保存固定公开文案，不把连接异常原文当业务证据。
- 新模块：`analysis_context.py`、`access_policy.py`、`material_reading.py`、`grounded_reporting.py`、`runtime_trust.py`、`trust_api.py`、`evaluation/`。connector 的 `request_observer` 是同步、每次实际 HTTP 前的授权/计量回调；生产不得降级绕过。
- 评估从固定旧代码运行真实 FastAPI/Deep Agents 入口，参考答案和 Agent 输入进程分离；保留首次失败、重试、未知分母及同冻结输入比较。R05/R06/R08/R09 已进入上方 M2 集成开发与分项验收，Case 与人工裁决分别留 R13/R12。

## 历史基础：Chat 运维工作台与用户历史

OpenSpec change：`chat-workbench-user-history`，规格与设计位于 `openspec/changes/chat-workbench-user-history/`。本节覆盖首版单用户相关描述。历史公开初始化见 `publish-public-repository`；当前公开副本及历史去标识化按已授权的 `sanitize-public-history` 执行。本文保留 API 契约，不包含现场配置或运行标识。

- 身份：`request.state.user={id,username,name,role}`；`identity.actor_id(request)`、`require_admin(request)`。用户 ID 只由服务端 session 恢复。旧无 owner 的数据仅归 admin，角色不是读取他人历史的特权。
- Runtime：`submit(payload, *, owner_id=None)`、`detail(id, *, owner_id=None)`、`message/cancel/retry(..., *, owner_id=None)`。内部兼容默认 admin；HTTP 必须传当前身份。payload 的 owner、conversation/thread/parent 关联不能越权。run、消息、AG-UI绑定的幂等均分用户。
- 会话：GET/POST `/conversations`；GET/PUT `/conversations/{id}`。GET 详情返回 conversation 字段、messages、runs（public run 列表）、attachments。POST `{title?,context?,client_request_id}`；PUT `{title?,context?,expected_revision?}`。
- 消息：POST `/conversations/{id}/messages` `{content,attachment_ids?,client_message_id,run_input?}` → `{message,run,conversation}`。消息先持久化；缺上下文保存为未执行并返回补充提示；幂等重发返回原收据。同上下文活跃轮次追加，否则创建关联新轮次。上下文改变不静默投入旧轮。
- 会话 ID：新 run 的 conversation_id/thread_id 等于服务端会话 ID；保存父 run 和有界历史上下文；会话关联 run retained，不能按原7天默认清空。
- 附件：POST `/conversations/{id}/attachments` `{name,media_type,content_base64}`；GET `/attachments/{id}` metadata；GET `/attachments/{id}/download` 授权下载。单件5MiB、单用户100MiB，请求实测上限8MiB。metadata不含path，binary允许保存但未实现解析明确标为未提取。
- 服务钩子：`app.state.attachments.reading_context(owner_id,conversation_id,attachment_ids)` 返回有界文本与未提取/截断元数据；`save_report(run)` 按run幂等。`app.state.conversations.finish_run(run)` 按run回写最终assistant消息与生成报告附件。Runtime从附件ID读取材料，不接受前端伪造材料正文。
- 画布复用持久GET与run级SSE；本次不增加会话级SSE传输。刷新只恢复订阅，终态输入框保持启用。
- Users：admin GET/POST `/users`、POST `/users/{id}/state` `{enabled}`；POST 创建 `{username,name,password,role:"operator"}`。GET `/session`返回真实身份，POST `/login`增加可选username，省略仍为admin。
- 权限：普通用户读可用配置、提交发布Skill分析、自己的历史与附件；共享配置变更、Secret/审计及用户管理仅admin。overview/queue/recent/invocations同样过滤owner。
- 分工：配置与持久化模块、执行器与协议模块、前端界面各自明确编辑范围；接口先对齐，集成与最终验收由变更负责人汇总。不并发编辑同一文件。

---

设计入口：[完整设计](docs/design/AIOpsStudio完整设计.md)（2026-10-27 同步 M2 分支，区分原实例、待验收实现与后续目标）；原 v0.5 总体及管理文档保留为来源。当前 API 契约以本合同、主规格和源码为准。产品语言 zh-CN；Python FastAPI + PostgreSQL，React/Vite + CopilotKit，Node 同源入口。源码目录为本目录；外部接入参数和私有运行资料不随公共仓库收录。首版真实数据接入，任何测试数据单独标记。

部署边界：应用、PostgreSQL、执行器与测试面向 Mac 本机；外部 CMDB、可观测和告警接口只读接入。端口、状态目录、数据库、可选 SSH 转发以及引导源配置均由部署者在仓库外提供，默认不启用转发。后续模型验证优先 MiniMax，不把历史模型结果改称为新模型验证。

更名兼容：Python 包为 `aiopsstudio`，环境变量主前缀为 `AIOPSSTUDIO_`，CopilotKit Provider 与 Node agent 注册名均为 `aiopsstudio`。旧环境变量由服务端集中兼容，同项新旧不同值拒绝；唯一旧状态目录和完整旧表可以复用，多目录/混合或不完整表要求明确处理，不能自动合并。自定义状态仅使用新键时必须提供外部数据库 URL 或完整数据库资产身份，不能因换键静默选择新库。有效旧 Cookie 在原有效期内升级，冲突返回 401 并清除新旧 Cookie。已有配置、制品和主密钥保持匹配；兼容不替代生命周期与完整恢复验收，细节见[完整设计第 13.3 节](docs/design/AIOpsStudio完整设计.md#133-当前-mac-部署)。

## API（均 /api 前缀）

- GET /session -> {authenticated,user}; POST /login {password}; POST /logout。服务端cookie会话，同源变更校验；错误 {detail,code?}。
- GET /overview -> {counts,queue,services,recent_runs,storage}。
- GET /skills -> {items}; POST /skills 创建草稿；GET/PUT /skills/{id} 读取/修订保存（PUT带expected_revision）；POST /skills/{id}/validate、/publish、/rollback、/state；GET /skills/{id}/versions。Skill字段：id,name,display_name,description,content,references(路径到文本),input_schema,dependencies(list),revision,enabled,current_version,versions,updated_at。发布版本是不可变快照。state body {enabled,blocked?}，rollback {version_id}。
- GET /mcp-servers -> {items}; POST /mcp-servers；GET/PUT /mcp-servers/{id}；POST /mcp-servers/{id}/probe、/discover、/publish、/state；GET /mcp-servers/{id}/tools -> {items}。配置字段：id,name,url,auth_type,secret_id,timeout_seconds,allowed_tools,revision,current_version,enabled,blocked,status,tools（工具name,description,inputSchema,outputSchema?,alias,readonly）。创建/更新可带secret_value（只写，绝不返回）。试调body见下。
- GET /model-configs -> {items}; POST /model-configs；GET/PUT /model-configs/{id}；POST /model-configs/{id}/publish、/test、/state。字段：id,name,provider,model,base_url,secret_id,temperature,max_tokens,revision,current_version,enabled,status；secret_value只写。
- GET /settings / PUT /settings -> 默认运行预算、默认模型ID、Ongrid API/证书/凭据引用、环境与身份映射；密钥只写或secret_id引用。GET /config-audit -> {items}。
- GET /alerts -> {items,total?,limitations}; GET /alerts/{id} -> 源告警详情及timeline/limitations。
- GET /runs -> {items}; POST /runs {skill_id,skill_version?,purpose?,title?,target?,incident_id?,analysis_start?,analysis_end?,message?,model_id?,environment?,client_request_id} -> run；GET /runs/{id} -> run完整详情（含messages,events,evidence,report）；POST /runs/{id}/messages {content,client_message_id,context?}；POST /runs/{id}/cancel；POST /runs/{id}/retry -> 新run/attempt并保留旧记录；GET /runs/{id}/events?after=N SSE持久事件。
- POST /tool-tests {server_id,tool_name,arguments,client_request_id} -> run（purpose=tool_test）；model /test -> run（purpose=model_test）。全部执行型任务同队列。
- GET /invocations -> {items}; GET /evidence/{id} -> 脱敏原响应与元数据。

Run字段：id,thread_id,attempt_id,purpose,title,status(queued/running/succeeded/partial/failed/needs_input/cancelled),skill_id,skill_version,config_revision,target,incident_id,analysis_start,analysis_end,created_at,updated_at,messages(list),events(list),evidence(list),report(object|null),error,usage,budget,cancel_requested。提交后配置快照在run内，敏感值只保存引用。

## Python模块分工

- backend/aiopsstudio/store.py: Store。get(kind,id)->dict|None; list(kind)->list[dict]; save(kind,id,data,expected_revision=None)->dict（增加revision），delete(kind,id)；atomic(callback)如需要；claim_job(owner)->run|None、renew_job(id,owner)、event(run_id,type,payload)->dict、events(run_id,after=0)->list。PostgreSQL JSON文档存储，claim使用数据库锁保证单执行槽；测试可用SQLite但正式验收必须PostgreSQL。StoreConflict异常。时间UTC ISO8601。save不在输出泄露密钥。store对敏感实体只供内部使用，API统一脱敏。
- backend/aiopsstudio/config.py + api.py + main.py：配置生命周期、认证、Secret、管理API（backend agent负责）。create_app通过configure_runtime(app,store)装配运行模块。Settings store kind='settings', id='main'。运行模块从app.state.store取得Store，从app.state.vault取得Vault（put(value)->secret_id,get(secret_id)->str,revoke(secret_id)）。kind: skill,mcp,model,run,invocation,evidence,audit,secret。实体全字段普通dict。
- backend/aiopsstudio/runtime.py：configure_runtime(app,store)注册alert/run/test/invocation/evidence API并启动单worker lifespan，或暴露async start()/stop()供main调用（runtime agent负责）。管理API probe/discover调用connectors模块接口，模型test通过runtime.submit。提交统一入口async submit(payload)->run。
- backend/aiopsstudio/connectors.py：async mcp_discover(config,vault)->dict{items,server_info,protocol_version}; mcp_call(config,vault,tool_name,arguments)->dict原MCP响应；async alert_list(settings,vault),alert_detail(settings,vault,id)；真实httpx/MCP官方SDK、严格TLS、超时限量（runtime agent负责）。
- web/：frontend agent负责。严格使用本合同API，公共api.ts集中容错，不用假成功或静态统计冒充后端。
- deploy/、README、集成测试、Node同源runtime与最终联调由主Agent负责。任何接口调整及时消息通知。

## 交付判据

Skill草稿到发布及指定版本调用；真实MCP发现和只读试调；告警选择到固定时间窗、真实模型Deep Agents分析、CMDB/遥测证据及报告；持久消息、取消/重试、浏览器刷新不重复执行；真实调用失败明确保留。GLPI显示未接入。源码/锁文件、运行部署、影响相称测试与真实浏览器验证一并交付。
