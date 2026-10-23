## Context

完整设计第 7 章与 R02 要求身份、时间和覆盖由服务端确定性核验。现有配置为任意 JSON，旧 run 不具备新契约；不把现有配置存在等同映射已验证。

## Goals / Non-Goals

目标为可存储、可通过 API 返回的版本化上下文和无副作用绑定函数。复杂源历史未知保持 unknown；不修改源、自动补造映射、推断健康基线或编造历史拓扑。

## Decisions

1. `freeze_context(payload, settings, captured_at=...)` 返回 `analysis-context/1`。`identity_mapping={version,records}` 中每条记录提供 source_id/object_type/native_id、canonical 的 environment/cluster/namespace/type/id、mapping_source、verified_at、valid_from/valid_to。空 cluster/namespace 仅表示该维度不适用；显示名称不进入主键。完整 canonical 字段的 JSON 数组派生稳定 SHA-256 key，不能按名称跨范围合并。
2. 优先 `target_identity` 完整键，同样必须由服务端 records 验证，不接受用户自报原生源映射。兼容 `target` 字符串在输入约束范围中唯一匹配 canonical id、原生 id 或显式 alias。无匹配/多个完整键/相同源与类型冲突 native_id 为 needs_input；不静默选择首项。缺历史映射有效期或验证时间同样 needs_input，记录 historical_validity_unknown，不把 verified_at 代替 valid_from。绑定 baseline 等具名窗口时再次检查该窗口是否落在映射有效期内。
3. `windows` 保存 fault 必选，baseline/change/validation 可选；每个窗口有绝对 UTC 起止和 reason。基线 comparability 默认 unknown，不能按时间先后推断健康。最长七天，零长窗口保留兼容。`source_offsets` 明确 offset 秒数及 unknown；无 offset 时不按零伪装实测。
4. `query_catalog={version,entries}` 每条以 source_id/tool_name 唯一，并指定 definition_digest、version、kind、window、identity_parameters、time_parameters、time_format、static_arguments。额外可变参数须明确登记在 allowed_parameters；未登记参数不进入源请求。目录中未知工具不允许猜参数。`bind_query` 校验工具摘要和快照版本，用完整来源映射锁定对象参数，时钟偏差有记录地投射为源窗口；传入冲突参数拒绝。目录可为 object_scope=none 的发现工具，但该类型不能据此证明目标对象或全局覆盖。
5. 任意 query/expression 类参数若用于对象查询，必须通过 `static_arguments` 或 `argument_templates` 冻结；模板仅支持显式的 `{native_id_json}`、`{canonical.id_json}` 等 JSON 字符串标量，避免原生身份被当作查询代码。目录配置的精确查询固定，模型不能另换目标。非结构化查询未登记时拒绝。
6. `coverage_record` 独立记录 source、declared/observed window、原始状态、分页和截断、时钟偏差及历史能力。只对声明范围给 complete；成功空列表为 empty，拒绝/异常不转为空；越界降为 partial 并标记 out_of_window，不能成为窗口内事实。源时钟偏差未知时不能将观察时间减零后冒充已归一化；观察点窗口关系保留 unknown。topology/historical=false 或未知均不能说明历史拓扑。即使目录声明 historical=true，topology 没有显式时间参数仍将 historical_support 冻结为 null，历史覆盖为 unknown；不根据 valid_from/valid_to 名称猜工具支持回看。
7. `legacy_context` 返回 unknown、legacy_unverified，不重写旧存储。所有函数深拷贝调用输入，不访问 DB/文件/网络，不读取当前墙钟以外的外部状态。
8. 查询绑定的 canonical_key 表示查询范围，不能自动赋给每个返回值。可选 `result_identity` 必须有 version、相对身份 pointer 和 mapping（native_id 或 canonical.id）；数组/多序列还须有显式 collection 指针。scope_fields 中的逐行指针或管理员审核的 fixed_scope 必须明确覆盖 environment/cluster/namespace/type 四维，并与 binding.mapping.canonical 精确一致。两类范围字段不能重复。报告核验按 claim pointer 定位对应行，匹配真实身份值；缺规则、缺维度、值冲突、以 singleton 文档身份冒充数组行时，保留 query_scope，fact.object=null、verification.object=unknown，只能作为有限保存材料，不确认该目标异常。禁止根据 service/job/instance 等标签名称推断其身份语义。
9. 内部快照保留完整映射、目录、源偏差、内容摘要和版本供后续确定性绑定。`Runtime.public` 去除 query_catalog、source_offsets、identity.mappings/candidates 以及配置、策略、历史材料和后端路径；运行详情只展示选中 canonical、状态、窗口、版本/摘要和澄清 issues。管理配置须走现有管理员入口，当前已授权证据只保留其具体查询绑定。客户端不能通过 HTTP 修改已固定上下文。
10. 身份问题在提交时结束为 needs_input，保存澄清提示，且不执行源或模型。身份 ready 不代表查询目录已登记；缺目录的工具在 bind_query 拒绝，不把这种情况伪装成可用查询。无业务身份的工具试调仍受来源授权，覆盖维持 unknown，不能证明业务目标完整。

## Interfaces

- `ContextError(code, detail)`：无效窗口、目录或参数的确定性拒绝；运行时转为现有 HTTP/工具错误。
- `freeze_context(payload, settings, *, captured_at=None) -> dict`。
- `bind_query(context, source_id, tool_name, arguments, *, definition_digest=None) -> {arguments,binding}`；binding 保存目录条目版本、工具摘要、canonical key、映射快照、投射窗口、历史能力及显式 result_identity（如已配置）。
- `coverage_record(binding=None, *, status='unknown', item_count=None, observed_start=None, observed_end=None, pagination_complete=None, truncated=False, stale=False, historical=False, error=None) -> dict`。
- `legacy_context() -> dict`。
- `validate_configuration(identity_mapping, query_catalog)`：校验管理 API 新格式，但旧格式只作为未验证旧配置保留。

## Risks / Trade-offs

严格目录会使未登记工具停止自动对象查询；这是明确缺口，不自动放宽成启发式。有效期仅在源有证据时填写；未知历史关系会降低覆盖而不会阻塞所有当前对象查询。配置版本和实际内容摘要同时冻结，阻止复用版本号时悄悄改变 attempt。

## Validation

合成案例覆盖跨环境同名、同范围歧义、缺映射、重建对象有效期、UTC/偏差/基线、冲突参数、目录/工具版本、分页/空/权限/超时/越界、无时间绑定的历史拓扑降级及旧格式降级。运行时合成检查确认歧义发生在 connector/model dispatch 前；API 检查新旧管理配置与授权读取，工作台 fixture 展示覆盖缺口。独立复核补充无关返回对象、篡改材料、片段原始坐标和出站边界回归；具体证据和未完成业务验收见 verification.md、review.md。

## Configuration Example

下例为合成配置，不是任何部署的源目录。definition_digest 必须换成该受控只读工具真实已审核定义的摘要；示例不可直接作为接入成功依据。

```json
{
  "identity_mapping": {
    "version": "mapping/1",
    "records": [{
      "source_id": "metrics",
      "object_type": "service",
      "native_id": "source-service-id",
      "canonical": {"environment": "test", "cluster": "cluster-a", "namespace": "payments", "type": "service", "id": "service-1"},
      "aliases": ["api"],
      "mapping_source": "reviewed-map",
      "verified_at": "2026-01-01T00:00:00Z",
      "valid_from": "2025-12-01T00:00:00Z",
      "valid_to": null
    }]
  },
  "query_catalog": {
    "version": "catalog/1",
    "source_offsets": {"metrics": {"seconds": 0, "reason": "synthetic example"}},
    "entries": [{
      "source_id": "metrics",
      "tool_name": "query_service",
      "version": "query/1",
      "definition_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "kind": "telemetry",
      "historical": true,
      "window": "fault",
      "identity_parameters": {"target_id": "native_id"},
      "time_parameters": {"start": "start", "end": "end"},
      "time_format": "rfc3339",
      "allowed_parameters": ["step"],
      "result_identity": {
        "version": "response-identity/1",
        "collection": "/rows",
        "pointer": "/source_id",
        "mapping": "native_id",
        "fixed_scope": {"environment": "test", "cluster": "cluster-a", "namespace": "payments", "type": "service"}
      }
    }]
  }
}
```

该响应示例要求管理员事先核实该源分区固定属于四维范围，且每条 `/rows` 记录都有真实 `/source_id`。固定分区不能由模型自报；源实际跨分区时必须改为逐行 scope_fields，不能复用示例把其他对象绑定进来。若尚无可验证响应身份规则，应省略 result_identity 并明确对象 unknown。
