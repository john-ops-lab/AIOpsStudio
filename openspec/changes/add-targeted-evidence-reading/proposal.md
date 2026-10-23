# Proposal

## Why

M1 的证据与Skill资料定向补读是完整设计 R03/R04 的第一优先交付。已有响应保存和文本引用不能保证实际读到尾部或事实正确。

## What Changes

新增受限材料读取、片段锚点、确定性报告核对及中文状态展示；复用已有owner、快照、队列和制品。只读源边界不变，模型验证优先MiniMax。

## Capabilities

### New Capabilities

- `targeted-reading`：证据与Skill资料定向补读。

### Modified Capabilities

无。通过新能力补强既有执行路径，不把历史结果升级为新验收。

## Impact

后端独立模块、runtime/API集成、中文工作台、合成/隔离HTTP与模型验证及完整设计状态。所有开发测试在Mac独立工作区，不改原实例与真实配置，不实现R12/R13。
