# M1 独立复核记录

日期：2026-10-23。范围为本次 M1 的运行时、来源/模型出站、对象/时间上下文、材料补读、结构化报告及其 API/UI。审查以合成数据、临时存储和真实 SDK 配合 mock transport 进行；不修改原运行实例，不调用真实源或模型。本记录不扩大到 M2，也不是业务验收结论。

## 发现与修复

以下问题均已报告集成负责人，并由相应模块负责人修复。测试位置为 [test_review_regressions.py](../../../backend/tests/test_review_regressions.py)、[test_connector_authorization.py](../../../backend/tests/test_connector_authorization.py) 和 [test_material_reading.py](../../../backend/tests/test_material_reading.py)；仓库根路径以文件名为准。

| 问题 | 可复现触发与影响 | 最终边界 |
| --- | --- | --- |
| 预组装材料跨越后续 egress 收紧 | 先组装获准上下文，再将当前策略拒绝 context；旧文本 hash 仍可能放行实际 HTTP | 准备文本同时绑定当前 policy snapshot digest；实际 HTTP 要求与最新策略严格一致，变化即拒绝并重建 |
| 模型 extra_body 未经类型出口 | messages/tools 已核验，但 extra_body.user 等额外顶层字段可携带未登记材料 | 除 messages/tools 外的模型参数统一通过 typed model_parameters，未知字段拒绝；未授权材料零传输 |
| 正确数字认证错误模型原句 | claim 引用真实 pointer/value，原句却声称其他对象损坏或确认根因 | 程序生成 text 仅说明结构化事实；model_text 与未核验 wording_status 单列，UI 不把原句放进证据支持的正面组 |
| 查询目标冒充每个返回对象 | 请求正确目标，但返回无关序列或 data.facts.subject；真实数值仍可能被归给查询目标 | 显式冻结 result_identity，按实际行、身份与四维范围匹配；query_scope 与 fact.object 分开，缺规则或不匹配对象保持 unknown |
| 当前 topology 被当成历史 | 目录 historical=true，但工具未绑定时间，当前 relation 被用于旧故障窗口 | topology 无显式 time_parameters 时 historical_support=null，历史 coverage=unknown，不能确认故障时关系 |
| 普通模型 ledger 绕过证据 hash | 保存后修改证据文件；补读能拒绝，但 ledger 直接读路径会把新字节送入上下文 | ledger、限制处理和模板三条读路径统一 MaterialReader.native，复验 hash、路径与大小，篡改 marker 不能进入上下文 |
| MCP 同源重定向带凭据转到新 path | 当前 SDK 会在 follow_redirects=false 下跟随同源 307/308；只检查原配置 URL 无法挡住新 path | transport 冻结完整 scheme/host/port/path/query，所有 3xx 拒绝；Ongrid 仅允许固定业务 URL 和准确 login URL；observer 在每次网络前执行 |
| 片段 anchor 支持未读字段或错行 | 字段投影后引用被省略字段，或时间过滤仅返回原始第二行却引用原始第一行 | receipt 保存 source_indices/source_total_items/projection_fields；核验使用原始坐标和实际字段，旧过滤 anchor 缺元数据拒绝，全集聚合须完整无筛选 |
| 本地撤销凭据移出出口禁值表 | 凭据在本地 revoke 后，用户误粘贴同一值；本地撤销不代表远端轮换，仍可能实际出站 | 出口禁值从 Vault.sensitive_values 取得包括已撤销值；撤销凭据不能使用，误粘贴材料也不能外发 |

返回对象契约由 R02 验证/冻结目录、R04 核验实际数据共同完成；不能只登记响应字段而忽略来源分区。`fixed_scope` 必须来自管理员审核的实际固定分区，跨分区源应使用逐行 `scope_fields`。缺信息时只保留 saved_material_only，不能靠标签名字补齐。

Reader 另修正完整响应包计量：metadata、binding 和 usage 与 data 一同计入 UTF-8 字节；在原子计费内迭代求稳定 usage 大小，单返回不超过 16 KiB，超出要求缩小 selector。该限制只约束读取范围，不改变源查询的 coverage 状态。每次 read 的完整 result 固定保存在 receipt，供下一轮模型回放。

## 首败与复验

- 篡改普通 ledger、投影/时间错 anchor、撤销凭据误粘贴均保留了修复前的失败复现：原先分别出现 marker 进入 ledger、anchor 错误放行、实际 SDK mocked HTTP 未拒绝。
- egress tightening 与 extra_body 独立回归首次执行时相关修复已落地并通过；没有把它们描述为已保留修复前红灯。重定向风险由已安装 SDK 行为和真实 SDK mock-transport 回归确认；修后只观察到原 endpoint 请求，未跟随 Location。
- 早期 connector 测试的 eager mock response 未经异步 stream 读取，无法验证流量上限；改用真正 AsyncByteStream 后验证实际 bounded 读取，未放松生产限制。
- 最后定向 bundle 为 159 passed、1 failed；唯一失败是新增模型测试替身的 LangChain 方法签名错误，10 项独立 review 回归全部通过。集成负责人已修签名并报告单例通过，全量冻结结果由 M1 总验证记录给出。
- 最终前端构建通过，保留现有 chunk 大小提示。独立 Chrome fixture 已检查覆盖状态、片段读取/下一页、拒绝访问和策略保存；最后一版模型原文/对象身份标签有编译验证，不冒充新增浏览器验收。

## 已同步契约

- [R02 设计](design.md) 和 [analysis-context 主规格](../../specs/analysis-context/spec.md)：完整身份/窗口、result_identity、无时间 topology 降级及公有 context 去除全局映射。
- R04 grounder：返回身份核验、结构字段与模型原句分层、片段原始坐标；来源与出口策略、history 递归授权及 API 安全投影由各自 change 和集成记录维护。

## 验收边界

本复核证明已列触发在合成协议/存储路径上受到确定性约束。未核验真实源记录/响应映射的现场正确性、真实历史能力、业务分析结论、移动端或用户接受。真实 MiniMax 结果继续按主流程保留 partial，不能将 report 生成、HTTP 成功或合成测试通过写成完整业务验收。
