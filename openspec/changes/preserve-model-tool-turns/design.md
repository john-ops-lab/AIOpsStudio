## Context

当前 LangChain `ModelRequest.override(messages=...)` 替换全部消息。实际 SDK 仅发送替换后的消息，graph 保存的 assistant/tool 不会自动重现。固定版本 langchain-openai 的 ChatOpenAI 同时在解析和序列化方向丢弃兼容供应商的 `reasoning_content`；MiniMax 文档要求保留完整工具往返。

## Decisions

1. 每次 `_investigate` 创建独立的内存协议缓存，绑定 run/attempt。只记录此次 handler 返回的 AI 工具请求，并从下一轮 graph 消息中匹配全部 tool_call_id 对应返回。拒绝不完整、重复、跨组或不匹配返回；不从任意会话历史恢复协议。
2. 最多保留最近两组完整往返，总序列化字节不超过 48 KiB；只能整组淘汰。单组超限或最新组不完整时停止探索进入禁工具报告，不剪切 JSON、推理或单条工具返回。上下文仍有持久证据 ledger。预算估算计入实际兼容 SDK 序列化的协议字节。
3. 在请求组产生前固定完整最新 source + egress 策略摘要。下一轮组装及最终 HTTP 前均校验摘要，变化即不重放；不能把旧 source_evidence 正文装进 model_protocol/content 来绕过字段收紧。新的禁工具报告重新投影 ledger；源撤权不能借此继续披露。
   全量冻结评估揭示 `_model_reading_view` 仅做有界裁剪，不构成类型投影；先前直接保留它生成的 ToolMessage 会被最终 HTTP 的别名一致性校验拒绝。正式源工具成功和错误返回统一从已保存证据重建单条既有 ledger，再按 `source_evidence` 投影，投影前后重验材料授权。只将投影后的条目作为工具结果，不能从原记录补回被 drop 的字段；必要的 evidence_id 被去除或改写时拒绝。补读结果仍按 `reading` 投影，不换用宽泛文本通道。原始证据、源参数及冻结策略保持不变。
4. 小型 ChatOpenAI 子类仅适配 `reasoning_content` 的入站保存/出站回传，不改变模型/端点/参数。只有字符串形式受支持，不自动请求或放行该字段。HTTP 仍逐字段执行 model_protocol、凭据及目的地检查。响应推理先进入本 attempt 私有内存，再从返回 graph 的副本剥离；正文中的 think 标签也从持久副本和可见事件移除。未登记推理字段时明确失败关闭。
5. 每个分析模型回合的 SDK 请求与阶段 timeout 同受 180 秒上限约束。探索回合取 `min(180, deadline-now-report_wall_seconds-1)`，即使仍绑定工具也允许模型直接形成完整报告；该轮超时后停止探索，使用已预留时间进入禁工具报告。禁工具报告取 `min(180, deadline-now-1)`；剩余不足时不派发。模型/输出 token/回合及总 wall budget 不增加。独立协议样本维持既有 55 秒上限。取消前后比较实际 HTTP 派发计数；已派发但未拿到 usage 时保留已知累计数并标记 token_usage_known=false，未派发取消不伪称供应商消耗。

## Validation

使用真实 LangChain/DeepAgents 编排与 httpx MockTransport，无真实模型调用。无内部调用计数驱动的合成模型根据收到的完整 assistant/tool 配对返回下一步；捕获实际第二轮 JSON 及 timeout 参数。覆盖多工具配对、完整组有界淘汰、源/字段撤权、组装后 HTTP 前策略竞态、未准入推理字段、推理不进入 checkpoint/事件、预算估算和取消计量。保留旧实现首败与修复结果，不修改固定评估 fixture/oracle。

## References

- [MiniMax OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api)：完整 assistant/tool 及 reasoning_content 的连续性要求。此次实际模型配置由测试方记录，不将其他型号要求泛化为验收结论。
