# 有界模型工具连续性验证

## 范围与首败

- 原真实 MiniMax 端到端首轮保留失败：四轮重复源读取；第五轮禁工具报告约 55 秒中断并生成 `platform_template`，不能算完整端到端通过。该次运行不重写，完整状态见 [PR 15 审查与升级验证](../../../docs/validation/2026-10-23-pr15-review-upgrade.md)。
- 本次独立审查先复现两项旧实现缺陷：列表文本块中的 `<think>` 内容进入可见文本；实际 SDK 请求已派发后取消，`token_usage_known` 仍为 true。派发前取消保持零已知用量的对照通过。
- 本次开发仅使用 Mac 隔离测试数据库、真实 LangChain/DeepAgents/ChatOpenAI SDK 和 `httpx.MockTransport`；没有调用真实模型或业务源，没有修改真实配置、历史、附件、评估冻结材料或 oracle。

## 定向结果

命令：`backend/.venv/bin/pytest -q backend/tests/test_model_tool_continuity.py backend/tests/test_model_continuity_review.py backend/tests/test_runtime.py backend/tests/test_runtime_trust.py`

首次协议修复定向结果为 **60 passed**。补齐首次源工具返回类型边界后，同一命令最新结果为 **63 passed**：新连续性与独立安全检查共 22 项，其余为受影响运行时和实际出站回归。

- 模型合成响应根据实际收到的协议推进，不依赖私有调用次数。正常路径只读一次源，两次模型 HTTP 请求形成报告；第二轮实际 JSON 为 system/user/assistant/tool，保留 ledger、工具参数、匹配的 tool_call_id 和显式准入的 reasoning_content。
- `InMemorySaver` 的所有 storage、writes、blobs，以及最终 graph、调用记录、事件和报告均无合成推理标记；不是仅检查最终清理后的 graph。
- 多工具结果完整成组；最多两组、总序列化不超过 48 KiB；缺返回、重复、单组超限、其他 attempt、旧策略摘要均不能部分重放。新 attempt 不从已有 graph 消息恢复缓存。
- 未登记推理字段、推理字段含凭据、旧工具正文在 source_evidence 字段收紧后重放，实际 HTTP transport 派发数均为零。即使重新批准 human 文本，也不能批准旧工具正文。
- 实际 SDK 请求的 read timeout 与阶段 timeout 一致；探索保留报告时间，禁工具报告受剩余 deadline 和 180 秒上限约束。剩余 35 秒时报告不超过 34 秒，剩余充分时不再受旧 55/60 秒限制；timeout 不进入模型 JSON 参数。
- 对第二轮带工具请求模拟在途超时：保留第一轮已知用量，取消调用标未知，之后在原预算内完成禁工具报告。额外验证派发前取消不制造未知消耗。
- 完整协议字节进入下一轮输入估计，足以侵占报告 token 预留时提前停止；没有增加模型调用、输出 token 或总 wall budget。
- 独立实际 graph/SDK 检查确认：成功源回复先执行 source_evidence 的 identity 别名和邮箱 drop，第二轮模型请求包含投影结果及原事实数值，原始证据 JSON 完全不变；源结果出现未登记字段时不派发第二轮。错误源回复同样删除 error/message，必要 evidence_id 被策略删除时拒绝，绝不从原记录补回。

## 测试修正记录

主集成第一次完整冻结评估得到 **86 passed / 15 failed / 4 unknown**（18 案例；此前候选为 102/0/3），该次观察保留。正常例已成功保存源证据，但第二次实际 HTTP 派发前以 `policy_egress_unprojected_protocol` 拒绝；不是合成模型或超时接口不兼容。新增往返保留误将只做裁剪的 `_model_reading_view` 当作已完成类型投影的工具结果，正文中的原始对象标识在最终守卫执行别名投影后发生变化。修复方向是在首次正式源工具返回处执行既有 `source_evidence` 单条 ledger 投影，成功和错误路径一致；不修改冻结策略、源 fixture、模型适配器或 oracle。该首败不被后续复验替换。

此类型边界的独立 SDK 回归在修复前为 1 failed / 1 passed，修复后两项通过。相关定向整组首次 61 passed / 2 failed：新错误路径测试未先执行正常 graph 的上下文别名准备，旧裁剪测试则把未登记 effective_query/scope 的原文当作正式工具回复。前者补全真实调用前提；后者保留原始证据和裁剪断言，先确认正式回复被拒绝，再直接调用纯裁剪方法验证。没有修改策略或源 fixture。

冻结复验的首次命令将旧候选摘要传给仅接受固定原始提交的 `--compare-with`，CLI 在执行案例前拒绝；没有产生观察。清理该次创建的空输出目录后，改为复用相同 M1 固定原始基线；评估器和原始结果未改写。

修复后完整 18 案例复验：**102 passed / 0 failed / 3 unknown**，严格通过 **15/18**。与先前可信候选逐项断言结果一致；suite、冻结材料、评分器和模型适配器的可比性信息一致。剩余未知仍为共享撤权、人工裁决和一致恢复，不写成全部能力通过。此复验新增观察，未覆盖或替换 86/15/4 首败。

新增阶段计时观测器首轮误把 `Runtime.execute` 的总尝试 deadline 也作为模型阶段计入，导致两个新增测试失败（其中超时注入提前命中第一轮）。修正观测器仅统计有界模型阶段后，原断言通过；未修改产品实现、固定评估数据或业务 oracle 来消除失败。

OpenSpec 全量严格检查首轮由主集成记录为 27 项通过、1 项失败：`model-tool-continuity` 主规格的 Purpose 不足 50 字符，触发严格模式失败。已仅补全 Purpose 的适用范围、授权和预算边界及验收限制，产品代码和测试保持冻结。执行 `openspec validate model-tool-continuity --type spec --strict --no-interactive`，目标规格严格复验通过，退出码 0。

## 独立复核与未完成边界

独立安全审查已复核字段准入、凭据、最新策略竞态、attempt 隔离和中间持久状态；独立 9 项检查通过。主集成对最终冻结代码完成全套 **481 passed / 18 subtests passed**，OpenSpec 严格检查 **28 passed / 0 failed**。发布和真实 MiniMax 后续尝试由主集成记录；真实复验须使用单独记录的新测试策略版本显式准入 reasoning_content，不修改首败材料或公开冻结评估集。当前合成结果不代表真实模型、业务源或用户接受已经通过。
