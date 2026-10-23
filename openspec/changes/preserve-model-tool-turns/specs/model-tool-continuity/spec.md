## ADDED Requirements

### Requirement: 当前尝试内的有界完整工具往返
系统 SHALL 在重建类型化证据 ledger 后保留当前 attempt 实际产生的最近完整 assistant 工具请求与其全部 ToolMessage 返回，最多两组且序列化总量不超过 48 KiB。系统 MUST NOT 从旧会话、其他 run/attempt 或任意 checkpoint 补入工具协议；不得拆断多工具组、无限追加历史或通过增加总预算弥补上下文丢失。

#### Scenario: 工具完成后的下一轮
- **WHEN** 模型调用一个或多个工具且返回已持久化
- **THEN** 下一次实际 SDK 请求包含对应 assistant/tool 角色、工具名、参数和匹配的 tool_call_id，以及当前类型化 ledger；不因重建上下文而丢失工具已完成的事实

#### Scenario: 超限或不完整组
- **WHEN** 旧组超出数量或总字节界限，或最新组不完整/单组超限
- **THEN** 仅淘汰完整旧组；最新组不能安全发送时停止探索进入禁工具报告，绝不发送孤立工具返回

### Requirement: 协议重放服从最新策略
系统 SHALL 将组绑定到其生成时的完整策略摘要，并在重放组装及最终 HTTP 前再次校验。协议字段 MUST 通过已冻结和最新出站策略；旧工具正文不得借字符串通道绕过源证据字段收紧。

#### Scenario: 首次返回与保存视图并不等于出站许可
- **WHEN** 正式源工具成功返回或保存了源错误结果
- **THEN** ToolMessage 正文 SHALL 由单条已保存证据的 source_evidence 类型投影生成，投影前后重验材料授权；别名和 drop 规则适用于首次返回，不能只裁剪原文后按 model_protocol 文本发送，不能从原记录补回被删除的字段

#### Scenario: 正文许可收紧
- **WHEN** 工具返回后 source_evidence 或 reading 字段被 drop/deny，或实际 HTTP 前策略变化
- **THEN** 旧组不再发送；新的禁工具报告只能使用重新投影的材料，源权限撤销时仍拒绝整个披露

### Requirement: 供应商推理协议与可见材料分离
系统 SHALL 在已配置 OpenAI 兼容接口支持字符串 reasoning_content 的解析及回传，并保留完整工具协议；该字段 MUST 由 model_protocol 显式准入，不得自动扩展策略。推理 MUST 仅存于当前 attempt 内存，并在进入 graph checkpoint、可见事件、日志、报告和附件前移除。

#### Scenario: 显式允许与未登记字段
- **WHEN** 供应商返回 reasoning_content
- **THEN** 已登记字段可在后续同 attempt 的完整工具组中回传；未登记或含凭据的字段在实际 HTTP 前拒绝，不能静默删除后宣称完整协议兼容

### Requirement: 原预算内报告与中断计量
系统 SHALL 将完整协议字节纳入输入预算估算，并将分析模型每轮 SDK 与阶段等待限制在 180 秒及原 deadline 剩余时间内；探索回合额外扣除既有 report_wall_seconds 预留，禁工具报告扣除一秒收尾。系统 MUST 保持模型、总预算、回合、输出 token 上限不变。

#### Scenario: 绑定工具的回合直接返回最终正文
- **WHEN** 模型收到完整工具结果后仍通过带工具的请求生成报告
- **THEN** SDK 与阶段使用相同的有界时间，不再被旧 60 秒默认值提前截断；本轮超时则停止探索，使用原剩余预算进入禁工具报告

#### Scenario: 剩余时间超过旧报告硬上限
- **WHEN** 原 attempt 剩余时间足够且报告需要超过 55 秒
- **THEN** SDK 和阶段 timeout 不再单独以 55/60 秒截断，仍不超过 180 秒和原 deadline

#### Scenario: 已派发模型请求取消
- **WHEN** 实际 HTTP 请求已派发但在返回 usage 前取消或超时
- **THEN** invocation 保留 cancelled 状态和耗时，保留以前已知 token 计数，同时标记 token_usage_known=false；未派发取消不制造未知用量
