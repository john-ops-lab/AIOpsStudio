# R07 服务端策略接口

本模块不发网络请求、不读取 Vault、不初始化应用；每次检查重读当前 Store 策略。下列示例只用合成主体、无效域名和凭据引用，不含可用配置。

## 公共 Python 接口

```python
service = PolicyService(store)
snapshot = service.snapshot()  # 源/出站缺失项可保存 None；真实派发绝不放行
scope = service.authorize_source(
    subject_id="alice", source_id="inventory", tool_name="read",
    config={"url": "https://source.invalid/mcp", "secret_id": "source-ref"},
    arguments={"target": "svc-a", "limit": 10}, target="svc-a",
    environment="prod", purpose="investigation", snapshot=snapshot,
)
service.authorize_material({"owner_id": "alice", "access_scopes": [scope]}, subject_id="alice")
service.authorize_model(model_config, subject_id="alice", snapshot=snapshot)
view = service.egress_view(
    "context", {"target": "svc-a", "description": "svc-a latency", "latency_ms": 23},
    subject_id="alice", scope_id="conversation-1", model_config=model_config,
    snapshot=snapshot, credentials=known_secret_values,
)
arguments = service.resolve_aliases(model_arguments, subject_id="alice", scope_id="conversation-1")
service.validate_document("access_policy", draft)  # API 另外做 admin、CSRF、CAS 与审计
```

`snapshot` 是 `{schema:"policy-snapshot/v1",source:doc|null,egress:doc|null}`。snapshot 和 scope 都是服务端运行内部数据，不能直接加入公共 run、事件、普通导出或模型上下文。`policy_digest(snapshot)` 只生成审计摘要。缺策略、无法读取、无效、越权及出站拒绝统一抛 `PolicyError(code, detail)`，detail 为固定公开文案，不含端点、对象或原始源错误。

## 源策略

`access_policy/main` 示例：

```json
{
  "schema": "source-access/v1", "version": "source-1", "enabled": true,
  "grants": [{
    "subjects": ["alice"], "sources": ["inventory"], "tools": ["read"],
    "targets": ["svc-a"], "environments": ["prod"],
    "purposes": ["investigation", "tool_test"], "secret_ids": ["source-ref"],
    "destinations": [{"url": "https://source.invalid/mcp"}],
    "argument_scope": {"mode": "exact", "fields": {
      "/target": "$target", "/limit": {"enum": [1, 10]}
    }}
  }],
  "denies": []
}
```

- 六类 selectors 都必须显式给出；`*` 仅作为整个 selector 的全范围值，不是模糊匹配。deny 可省略任意 selector 表示全范围，空 deny `{}` 拒绝全部；不登记的 deny 条件报错，不能静默忽略。
- `argument_scope.exact` 登记所有实际叶字段；额外字段、缺字段、目标不一致均拒绝。约束支持 `$target`、`$environment`、`{"equals":固定JSON值}`、`{"enum":[固定值]}`。JSON Pointer `*` 仅匹配数组下标，不能放行任意对象键。
- `argument_scope:{"mode":"source"}` 必须同时 `targets:["*"]`，表示清楚授予整源工具读取权。run 的 target 不能让自由查询自动变成受限查询；有限目标用户使用自由查询时拒绝。
- `secret_ids` 必须精确等于固定连接的 secret_id/password_secret_id 去重集合，空认证显式填 `[]`。每个 HTTPS base URL（含 path）绑定；默认端口和末尾斜线规范化，其余目标不猜测。HTTP 仅 loopback 且 destination 明示 `allow_http_loopback:true` 才允许。
- TLS 关闭、follow_redirects、内嵌凭据、query/fragment 或未绑定 Host 均拒绝。loopback 转发的 Host 必须在 destination.host_header 显式一致登记。
- `authorize_source` 应在提交、领取、真实源请求前、返回保存/交付前重验。工具内多次 HTTP、登录和重试也需要 observer/hook。历史/派生材料保存 `owner_id` 与非空 `access_scopes`，每次读取按最新规则重验；旧无来源元数据拒绝。

## 出站策略与别名

`egress_policy/main` 示例：

```json
{
  "schema": "model-egress/v1", "version": "egress-1", "enabled": true,
  "model_bindings": [{"subjects":["alice"],"models":["model-1"],
    "secret_ids":["provider-ref"],"destinations":[{"url":"https://provider.invalid/v1"}]}],
  "types": {
    "context": {"fields":{"/target":"alias","/description":"text","/latency_ms":"allow"},"max_bytes":4096},
    "user_message": {"fields":{"/content":"text"},"max_bytes":64000}
  },
  "denied_types": []
}
```

材料类型由服务端分类，不能由附件、前端或模型自报。prompt、skill、user_message、history、attachment、tool_definition、source_evidence 分别分类并独立登记；日志、未知二进制、源错误没有默认正文许可。字段动作：`allow`（JSON 标量/显式空容器）、`text`（明确许可的文本）、`alias`（结构化标识）、`drop`、`deny`。未知叶字段拒绝，输入和输出均受字节上限约束。

每次投影同时通过当前与冻结模型/凭据/目的地及字段规则；当前 drop、deny 和 denied_types 优先，冻结版本也不能被最新新增许可扩大。凭据字段及已知真实凭据值即便误登记 allow 仍拒绝，不尝试把凭据发给模型后再清理。模型 HTTP auth header 在绑定目的地按 connector 规则携带真实 provider 凭据，正文始终禁止。

结构化 alias 字段先注册，再处理同材料文本内的完整标识 token；相邻 `svc-a10` 不会因为 `svc-a` 被错误替换。`egress_alias` 的随机映射按 owner + conversation/thread 隔离，同一会话跨轮保持一致。映射不编码原值，不出现在返回值或 policy snapshot；API 不得公开此 Store kind。

`resolve_aliases` 默认只还原整个 JSON 字符串等于本 scope alias 的值；先还原，再做 R02 服务端模板绑定和源授权。query 表达式内嵌 alias 或跨会话/未知 alias 拒绝，不猜原值。`text=True` 只用于已经通过读取授权的服务端报告展示，不得用于重写源查询。

## 集成验收责任

纯模块测试只证明规则语义。真实模型传输前仍需检查包含 system/user/历史、工具 schema/描述/返回、摘要、重试和异常的最终 HTTP payload，且连接重定向禁止携带凭据。HTTP 捕获测试要验证允许事实、别名一致、禁止标记缺失、模型未换、权限撤销生效，并检查日志/事件/导出；未执行不能宣称通过。Case 仅提供逐读者授权契约，R13 实际共享/撤回另验。

## HTTP API

- 管理员 GET `/api/access-policies/source`、`/egress`：未配置 `{configured:false,revision:0}`；已配置 `{configured:true,revision,policy}`。普通用户读写均 403。
- 管理员 PUT 同路径：`{expected_revision,policy:{完整策略}}`；兼容完整顶层策略加 expected_revision。初次 revision=0，后续 CAS；内容不变同版本可幂等保存，变更必须新 version，已出现过的 version 不能换内容。凭据只引用，拒绝已知原文及凭据字段；审计只记版本/摘要/修订。
- POST `/api/runs/{id}/material-reads`：`{resource_type:'evidence',evidence_id,selector}` 或固定版本 skill_reference 选择器；始终服务端 owner，按实际读取预算计量。GET `/api/fragments/{anchor_id}`：从当前用户真实 readreceipt 恢复原选择器，重验 run/attempt/owner/源范围、保存字节及锚点数据摘要，成功返回同读取结果形状；不能按路径、URL 或伪造片段读取。
- 统一错误：未认证 401；材料越权、未知/变化片段 404；选择器/上下文/策略格式 422；材料预算 429；缺策略或读取不可用 503；其余源/出站拒绝 403。正文固定中文，不返回内部源错误或源地址。
- MCP probe/discover 使用当前管理员的 `$catalog` 工具及 `purpose=discovery`，要求显式全源 grant。实际请求 observer、派发前和返回后核对当前 session/主体、policy、连接字段、阻止状态及凭据撤销；不允许 admin 隐式绕过。拒绝审计仅记录固定 code/purpose。
- settings 保存时校验新版本 identity_mapping/query_catalog 的完整格式；保留未版本化旧结构，不自动升级 ready，默认 environment 不再静默丢弃。

## 历史与派生附件

会话读取和列表按 conversation/thread/消息 run_id/active_run 关联的每个 run 重验当前源范围。撤权时整条会话从可见列表移除，详情拒绝，防止标题、旧 assistant 正文或 run 报告旁路。存在 run 却没有授权器时失败关闭；新建尚无 run 的空会话不依赖源策略。幂等创建仍先查原 owner/request key，不能因为旧会话隐藏而偷偷新建重复记录。

运行授权递归检查 history_context 的继承轮次；缺失/循环引用拒绝。有派生报告或助手正文却缺可信运行来源标记的旧记录拒绝，不按当前 owner 或新策略补造可信历史。

`configure_trust_api` 为附件安装服务端 authorize_run 回调。generated_report 或带 run_id 的制品在保存、列表、metadata、模型阅读与下载时重验原 owner 和该 run 当前源范围；列表隐藏被拒绝报告，下载/读取返回固定拒绝。管理员也不能读取其他 owner。下载另核对原保存大小/摘要。用户原始 upload 不自动继承源权限，只按 owner+conversation 校验；不能用报告附件或旧会话绕过源撤权。

## 普通用户选择入口

普通用户 `/api/settings` 只返回其可见默认模型 ID；Skill/模型目录只返回已发布启用的选择元数据，不能返回正文、references、完整版本、base URL 或凭据引用。MCP 列表/详情只有在当前主体具备最新 `$catalog` + discovery 全源授权时才返回 `{id,name,current_version,enabled,blocked}`；其他源列表隐藏、直接详情 404，版本读取 403。管理员仍可管理配置，但真实 probe/discover 始终另需源授权。overview 使用相同源可见规则和最新历史授权，不展示私有源服务、被撤权的 run 或全局数量。

升级前历史例外见 `preserve-legacy-history-on-upgrade`：仅显式清单登记且内容摘要匹配的原owner只读档案可读；不补造source scope、不准继承/补读/模型出站，未登记或变化旧记录仍拒绝。
