## Why

真实模型端到端首轮发现：探索请求每次重建为 system + user ledger，丢失 assistant/tool 往返；重复取证后，报告在剩余总时间充足时被 55 秒阶段上限截断。取消中的已派发模型请求仍可能显示 token 用量已知。这些结果保留为首败，不修改原案例、源 fixture 或独立 oracle。

## What Changes

- 在当前 attempt 中保留有界、完整成组的 assistant 工具请求和对应 tool 返回，继续发送按类型投影的全局 ledger。
- 兼容已配置 OpenAI 兼容供应商的 `reasoning_content` 往返，必须由现有出站策略显式准入；不自动增加字段许可。推理仅保留于当前执行内存，不进入 graph checkpoint、事件、报告或前端。
- 协议组绑定产生时的完整策略摘要；策略变化后停止重放，转用重新投影的禁工具报告，最新源撤权仍拒绝。
- 将协议字节计入模型输入预算；报告的 SDK 和阶段等待均受当前 attempt 剩余时间及 180 秒上限约束；已派发请求取消后标记用量未知。

## Capabilities

### New Capabilities
- `model-tool-continuity`: 有界工具往返、供应商推理字段兼容及报告阶段预算。

### Modified Capabilities
- 无；既有 source-access-scope 最新策略及类型出站约束继续适用。

## Impact

影响运行时模型消息组装和 OpenAI 兼容 SDK 适配；不改变公开 API、既有配置、总预算、固定案例、源查询或历史/附件。真实模型后续重验与合成 SDK 验证分别记录。
