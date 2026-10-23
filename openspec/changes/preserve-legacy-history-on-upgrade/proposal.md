# Why
M1 的严格来源授权会让升级前缺少可信来源元数据的旧会话和报告不可见。升级必须保留用户可读取的历史和附件，同时不能把未知旧材料当作新授权证据。

# What Changes
增加管理员离线、一次性登记的旧数据只读档案清单；绑定原记录 ID、owner 与完整内容摘要，单独保存，不改写原记录或附件。只读页面与下载保留，执行、模型上下文、补读和历史继承拒绝，页面引导新建会话。

# Capabilities
## New Capabilities
- `legacy-history-archive`: 有界升级清单与 owner 只读档案。

## Modified Capabilities
- `source-access-scope`: 只读档案例外不授予执行或模型披露权。

# Impact
历史授权、会话详情、附件下载、运行只读投影、升级脚本与工作台档案提示。
