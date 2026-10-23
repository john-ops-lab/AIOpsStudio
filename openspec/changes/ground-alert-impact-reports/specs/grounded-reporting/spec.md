## ADDED Requirements

### Requirement: 事实与模型解释分层
系统 SHALL 将源事实、潜在影响、实际观察、候选解释及未知范围分开，保存 report_version、fact_id、claim_id、精确片段和底层来源血缘。

#### Scenario: 报告引用与数字核验
- **WHEN** 模型提交含引用、对象、窗口、数字/单位/关系方向的结论
- **THEN** 确定性核对实际保存片段；不存在、错配或无法核验的结论降为待核实，时间相关不得升级根因

### Requirement: 查询范围与返回对象分开
系统 SHALL 将查询目标保存为 query_scope，只有冻结 `result_identity` 的版本、逐行/series 身份字段、来源 mapping 及 environment/cluster/namespace/type 四维范围全部精确匹配，才可赋 fact.object 和 verification.object=verified。缺失或冲突 MUST 保持 object=null、saved_material_only，禁止由查询目标推断每个返回值的身份。

#### Scenario: 返回第二个对象或不完整范围
- **WHEN** 返回序列/记录的身份与固定 mapping 不同，缺范围维度，或 singleton 文档 scope 被用于任意数组行
- **THEN** 原始值可形成有限保存事实，但该对象和相关 observation/normal/anomaly 不能被确认

### Requirement: 结构化核对不认证模型原句
系统 SHALL 分别保存模型原句 model_text、未核验 wording_status 和程序生成的结构化 text；数字相等和引用存在 MUST NOT 认证原句的因果、对象或业务判断。只有已登记的确定性规则能核对值、计数、单位、关系方向及实际查询窗口。

#### Scenario: 正确数字配错误因果
- **WHEN** 模型结论引用正确数字但原句声称另一个对象故障或确认根因
- **THEN** 仅结构化字段接受核对，原句保持未核验；根因仍 undetermined，不成为已确认业务结论

### Requirement: 单位与正常异常阈值来自冻结规则
系统 SHALL 从源或固定查询绑定取得单位，计数单位仅来自确定性基数规则，不根据指标名猜单位或补零。normal/anomaly SHALL 需要带版本、运算符、阈值和单位的冻结 assessment_rule；模型自报阈值、未知单位或未覆盖对象不得形成正常对照或已观测异常。

#### Scenario: 未登记阈值
- **WHEN** 模型声称某数值异常或正常，但无冻结规则或规则与值/单位不符
- **THEN** 保留原值和待核实状态，不把模型给定阈值作为自己的验证依据

### Requirement: 片段引用使用实际原始坐标和字段
系统 SHALL 根据保存 readreceipt 的原始 source_indices、source_total_items 和 projection_fields 验证片段引用。过滤视图 offset MUST NOT 冒充原始下标，投影后未读取字段不得由该 anchor 支持，过滤后缺新坐标的旧 anchor 必须拒绝；整数组聚合需要全部原始项且无投影/时间筛选。

#### Scenario: 投影与时间筛选
- **WHEN** 片段只返回 public 字段或时间筛选仅包含原始第 2 行，而 claim 引用 secret 字段或原始第 1 行
- **THEN** anchor_invalid 并降级结论；原始证据合法读取仍可按其独立来源范围核对

### Requirement: 完整性和反证
系统 MUST 保留正常对照、矛盾、第二序列和末端数据；源失败、陈旧、缺页及模型失败分别展示，同源转述不得计作多份独立支持。

#### Scenario: 有缺口的最终报告
- **WHEN** 来源不完整、核验失败或模型无有效结构化结论
- **THEN** 终态与中文界面明确部分/未知，保留模型建议或平台模板来源，不宣称完整业务通过

#### Scenario: 逻辑传输分段与完整原子页面
- **WHEN** 逻辑响应尚未收齐/整体核验失败，或仅有业务分页中的完整原子页
- **THEN** 前者不得引用内部片段确认事实；后者最多支持该页局部事实，同时保留全局 partial，不能推断完整名单或全局不存在

#### Scenario: 多来源转述与末端反证
- **WHEN** 多个工具转述同一底层来源，或后续样本与峰值/先前值不同
- **THEN** 来源按血缘保守归组，正常对照、冲突和每个返回序列的末端/极值都保留；末端低于峰值不自动宣称恢复正常
