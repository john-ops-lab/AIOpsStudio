# Design

## Context

R03 消费 R02 冻结上下文及 R07 最新材料权限，供 R04 使用实际读取的定位收据。全部实现和验证在独立 Mac 环境，源系统只读且本轮不调用实际业务源。

## Decisions

- `MaterialReader` 只接收当前 run/owner 的 evidence，或所选 Skill/通用 CMDB 方法不可变版本 manifest 内的 reference。正文必载，参考正文按需读取；拒绝任意路径、URL、未固定版本与已 blocked Skill。
- 读取保存文件先核对限定目录、绝对路径、无符号链接/非普通文件、最大字节和 SHA。普通 ledger、源限制、模板及最终事实核验同样复用此入口，不能等报告生成后才拒绝改写数据。
- JSON Pointer、offset/limit、fields、series 和绝对时间形成有界 selector。原始字节不修改，源覆盖、保存完整性及实际阅读范围分开。丢弃的字节和未收齐的逻辑传输不能通过补读恢复。
- `position.source_indices/source_total_items/projection_fields` 明确筛选前的原始坐标及实际字段。fragment 根据材料摘要、资源类型、位置、selector 和 data digest 稳定生成；不能用过滤后offset替代原下标，也不能引用投影外字段。
- 模型每包16KiB、每run16次/128KiB；人工每包16KiB、512次/8MiB。完整UTF-8返回包（含metadata与usage）计量，计数固定点收敛后原子保存，不是仅计data。估算token明确为估算；重复读取单独记账。
- 模型成功补读持久化有界result，下一轮上下文再次按最新出站策略投影。模型ReadingError保留安全提示供纠正，不把缺失材料当空集；PolicyError继续停止。HTTP人工接口保留404/422/429明确状态。
- POST `/api/runs/{id}/material-reads` 与 GET `/api/fragments/{anchor_id}` 使用同一Reader。fragment每次重新授权、校验原文件和selector及坐标，不返回服务器路径。读取收据、事件、owner与attempt关联保留。

## Risks

复杂未知JSON、过大单行/元数据、旧缺来源材料和不完整传输可能无法补读，要求更窄范围或明确未知。R03不补源端分页协议，不宣称日志/Trace穷举，也不提供Case共享和任意包导入。
