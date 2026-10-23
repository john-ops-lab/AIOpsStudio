## ADDED Requirements

### Requirement: 不可变材料与资源类型
系统 SHALL 仅允许可信 owner 读取当前 attempt 的保存证据，或固定 Skill/通用方法版本清单内的参考资料。Skill 正文必载，参考正文仅按需读取；任意路径、URL和可变草稿不得成为读取目标。

#### Scenario: 资料未请求时只见清单
- **WHEN** 分析加载已发布 Skill
- **THEN** 模型获得固定正文和reference manifest，不获得未请求参考正文；已请求正文保留版本与范围回执

### Requirement: 保存字节完整性
系统 MUST 在材料API、普通模型ledger、平台模板与事实核验读取前校验限定目录、无符号链接/普通文件、读取上限及保存SHA，失败不得把新字节交给模型。

#### Scenario: 文件在保存后被改写
- **WHEN** 当前路径字节与保存SHA不一致
- **THEN** 材料拒绝或保留不可读索引，改写内容不进入上下文或确定性事实

### Requirement: 精确选择范围与片段
系统 SHALL 支持有界JSON Pointer、行/字符偏移、字段投影、序列与绝对时间筛选，回执标记原始数组下标、源数组总量和实际字段。片段引用只能覆盖实际所读位置。

#### Scenario: 时间筛选或字段投影
- **WHEN** 视图第一行来自原始第二行，或只选public字段
- **THEN** 稳定锚点仍定位原始第二行，不能支持原始第一行或未读字段的引用

### Requirement: 分层完整性与预算
系统 MUST 分别保留source/storage/view完整性，模型与人工按完整UTF-8返回包原子计量次数、字节、估算token和重复读取；超额拒绝，不把未取得数据伪装恢复。

#### Scenario: metadata导致包超限
- **WHEN** data本身小于上限但加metadata和usage后超过16KiB
- **THEN** 拒绝并提示更窄选择器，读取账单不部分写入

### Requirement: 最新授权与下一轮回放
系统 MUST 每次人工、模型及片段重读核对owner与最新源范围；成功补读在下一轮有界上下文经最新出站策略重投影。模型选择器错误可以纠正，权限拒绝不得降级绕过。

#### Scenario: 已保存证据撤权
- **WHEN** 历史读取或片段导航发生在source授权撤销后
- **THEN** 不返回内容，也不利用旧回执或旧上下文继续发送材料
