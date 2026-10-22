# EasyOps 首版实现协作合同

## 当前变更：Chat 运维工作台与用户历史

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

依据：[总体设计](docs/design/EasyOps总体设计-v0.1.md)（正文 v0.5）与[管理设计](docs/design/工作台Skill与MCP管理设计-v0.1.md)。产品语言 zh-CN；Python FastAPI + PostgreSQL，React/Vite + CopilotKit，Node 同源入口。源码目录为本目录；外部接入参数和私有运行资料不随公共仓库收录。首版真实数据接入，任何测试数据单独标记。

部署边界：应用、PostgreSQL、执行器与测试面向 Mac 本机；外部 CMDB、可观测和告警接口只读接入。端口、状态目录、数据库、可选 SSH 转发以及引导源配置均由部署者在仓库外提供，默认不启用转发。后续模型验证优先 MiniMax，不把历史模型结果改称为新模型验证。

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

- backend/easyops/store.py: Store。get(kind,id)->dict|None; list(kind)->list[dict]; save(kind,id,data,expected_revision=None)->dict（增加revision），delete(kind,id)；atomic(callback)如需要；claim_job(owner)->run|None、renew_job(id,owner)、event(run_id,type,payload)->dict、events(run_id,after=0)->list。PostgreSQL JSON文档存储，claim使用数据库锁保证单执行槽；测试可用SQLite但正式验收必须PostgreSQL。StoreConflict异常。时间UTC ISO8601。save不在输出泄露密钥。store对敏感实体只供内部使用，API统一脱敏。
- backend/easyops/config.py + api.py + main.py：配置生命周期、认证、Secret、管理API（backend agent负责）。create_app通过configure_runtime(app,store)装配运行模块。Settings store kind='settings', id='main'。运行模块从app.state.store取得Store，从app.state.vault取得Vault（put(value)->secret_id,get(secret_id)->str,revoke(secret_id)）。kind: skill,mcp,model,run,invocation,evidence,audit,secret。实体全字段普通dict。
- backend/easyops/runtime.py：configure_runtime(app,store)注册alert/run/test/invocation/evidence API并启动单worker lifespan，或暴露async start()/stop()供main调用（runtime agent负责）。管理API probe/discover调用connectors模块接口，模型test通过runtime.submit。提交统一入口async submit(payload)->run。
- backend/easyops/connectors.py：async mcp_discover(config,vault)->dict{items,server_info,protocol_version}; mcp_call(config,vault,tool_name,arguments)->dict原MCP响应；async alert_list(settings,vault),alert_detail(settings,vault,id)；真实httpx/MCP官方SDK、严格TLS、超时限量（runtime agent负责）。
- web/：frontend agent负责。严格使用本合同API，公共api.ts集中容错，不用假成功或静态统计冒充后端。
- deploy/、README、集成测试、Node同源runtime与最终联调由主Agent负责。任何接口调整及时消息通知。

## 交付判据

Skill草稿到发布及指定版本调用；真实MCP发现和只读试调；告警选择到固定时间窗、真实模型Deep Agents分析、CMDB/遥测证据及报告；持久消息、取消/重试、浏览器刷新不重复执行；真实调用失败明确保留。GLPI显示未接入。源码/锁文件、运行部署、影响相称测试与真实浏览器验证一并交付。
