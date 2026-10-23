# legacy-history-archive Specification

## Purpose

保留升级前记录的原 owner 只读可用性，以显式不可扩容清单和内容摘要隔离来源未知的旧历史，避免旧材料被新执行或模型上下文隐式继承。

## Requirements
### Requirement: 显式冻结升级档案
系统 SHALL 仅通过离线一次性登记允许升级前旧记录进入只读档案；清单绑定原 ID、owner 和完整内容摘要，登记重验当前数据且不修改原记录与附件。

#### Scenario: 登记后新记录或已变化记录
- **WHEN** 未登记记录、内容变化或错误 owner 请求档案
- **THEN** 系统拒绝读取，不因缺失新信任字段而自动放行

### Requirement: 只读保留且不升级信任
系统 SHALL 为合法 owner 保留已登记会话、运行和原报告附件的只读读取，标为 `legacy_unverified`；MUST NOT 允许其用于模型上下文、补读、历史继承或新执行。

#### Scenario: 查看旧会话并继续分析
- **WHEN** owner 查看登记档案并尝试重试、发消息或补读
- **THEN** 只读历史和原附件可读，其余操作拒绝并引导新建会话；原记录和文件保持不变
