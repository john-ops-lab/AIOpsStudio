# Design

## Context

遵守完整设计与 R01→R02→R03/R07→R04 依赖，使用服务端固定上下文和最新授权。

## Decisions

- 原材料不可变；证据定位由服务端校验，JSON/序列/行范围有界，缺失与截断明确。
- source/storage/model-view三层完整性分别持久化，读取回执用于报告锚点；资料来自固定版本manifest，正文必载、references按需。
- 报告结构由保存证据和确定性规则核对；不支持的自由文本仅为未核验模型解读，不能当作已确认事实。数字/单位/关系/窗口不由模型自行认证。
- latest源撤权与出站禁止逐次优先；新策略/旧快照取约束交集，失败不放行。
- 分开记录代码/合成/真实模型/业务接受，原始观察和秘密留仓库外。

## Risks

大型单行、复合MCP响应、历史无完整元数据均可能不可完整补读，返回受限状态；不伪造恢复。任意源schema无法通用解释，未注册语义保持未知。

## Deterministic report contract

`grounded_reporting.ground_report(run, analysis_text, evidence_documents, material_reads=None)` 是纯模块。输入的 `(record,native)` 必须先由调用方完成当前 owner/源权限及保存字节摘要核对；函数本身不读取路径和网络。支持 JSON 对象或严格 JSON 文本 `{analysis,claims}`；普通文字、代码围栏或无 claims 的正文保持未核验模型解释和 partial，不从语句猜事实。

Claim 需明确 `kind/evidence_id/pointer/rule/value`，可带 unit/window/anchor_id/text/next_check。kind 为 observation/potential_impact/candidate/root_cause/normal/anomaly；规则为 scalar/count/min/max/first/last/relation，位置相对 `material_reading.logical_document`。fact ID 固定在保存证据坐标+规则，claim ID 绑定 report_version。根因始终 undetermined；拓扑只能 potential_only，normal/anomaly 需要保存记录/冻结查询绑定中的 assessment_rule(version/operator/threshold/unit)，不接受模型自报阈值。

自动解释仅支持明确 Prometheus success matrix/vector，逐序列保留 first/last/min/max/count，包含第二序列、尾部与正常对照。单位取源/冻结绑定；count 的 items 来自确定性基数规则，不拿时间或业务单位修饰计数。末端低于峰值只表示保存序列模式，不能自动称恢复正常。未知 schema 不硬解释；可用明确 scalar/关系坐标做局部核对，但不扩大业务语义。

输出包括 report_version/facts/claims/potential_impacts/observed_anomalies/counterevidence/unknowns/candidates/validation/analysis。validation 仅描述结构化结论核对，analysis_verified/business_acceptance 始终保留未验证。partial/未知/陈旧/历史不足及对象/窗口不一致限制结论；传输未完整、保存截断、源失败、明确校验拒绝不引用内部片段。完整原子页面可形成 saved_material_only 事实，不能表示全源完整。来源血缘缺失时按 source 保守归组，多个工具包装同底层不增加独立来源数；冲突值显式保留并降级相关结论。

### Returned object identity

查询目标只记为 fact.query_scope，不能自动成为返回 fact.object。固定查询绑定可登记 `result_identity={version,collection?,pointer,mapping,scope_fields?,fixed_scope?}`：collection 为返回行/series 数组的 JSON Pointer，pointer 相对单条返回对象读取身份；mapping 为 native_id 或 canonical.id。environment/cluster/namespace/type 四个范围维度须逐项由行内 scope_fields 或管理员已验证来源分区 fixed_scope 覆盖，且与冻结 binding.mapping.canonical 精确一致。返回标识和 source 也须匹配该 mapping；不能用整文档 scope 代替不同 series 的各自身份。

集合规则必须根据被引用的 claim pointer 选同一数组行；无 collection 的 singleton 规则不能给任意数组元素贴身份。缺规则、缺维度、异源、异对象、异范围或不确定映射时，fact.object=null、verification.object=unknown、scope=saved_material_only，保留原值与 query_scope，但不得成为 verified claim、已观测异常或正常对照。模型原句单独保留 model_text/wording_status，已核验 text 由程序从结构化坐标/规则/值生成，数值一致不等于模型任意解释获支持。

### 片段原始坐标

R03 的 position.source_indices 表示本次数组返回项在保存原数组中的下标，经过时间筛选、offset 和字节缩页后仍保留原始定位；source_total_items 是筛选前原数组长度，total_saved_items 仍是筛选后的分页总量。projection_fields 明确实际保留字段或 null，非数组的 source_indices/source_total_items 为 null。

R04 只接受 source_indices 覆盖的原始行及 projection_fields 覆盖的字段；视图 offset=0 不代表原始 row0。整数组 count/min/max/first/last 必须覆盖全部原始下标且无字段投影/时间筛选。缺新坐标且带投影或时间筛选的旧 anchor 保守拒绝，不能猜索引；无过滤的旧锚点仍可按原 offset/end 合同处理。某个 anchor 无效不自动删除原件，原件可经独立合法读取后另作核对。

### 运行与中文呈现

运行集成先使用当前 owner/源范围和材料摘要验证保存证据，再调用纯核对模块；普通模型上下文也必须核对原件摘要，不能只在补读时防修改。补读错误作为明确可修正的工具结果，保留未知，不能编造片段。报告将保存材料计算、结构化结论、登记关系上的潜在风险、遥测实测异常、反证、未知及候选分开；模型原句放在“未核验”折叠区。返回对象未知明确显示，不以查询目标代填。证据/精确片段入口使用 R03/R07 的重新授权读取流程。

代码存在和合成测试分别记录，浏览器流程、真实 MiniMax、真实业务源及用户接受由主集成总验证记录给出。未实现 R12 人工裁决和 R13 Case 共享，也没有把根因确认改为已完成。
