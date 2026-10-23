# Project Identity Specification

## Purpose

统一 AIOpsStudio 的产品展示、安装包、执行与协议入口、设计及公开协作身份。新安装使用新标识；兼容旧配置和持久数据时保留原有效期与资源归属，遇到歧义明确失败。仓库改名保持同一身份、历史和安全门禁，不因名称变化丢弃用户数据或私有扫描来源。

## Requirements

### Requirement: 统一项目标识
系统 SHALL 在当前产品展示、安装包、执行入口、协议 agent、设计文档、仓库及现有项目 Issue 中使用 AIOpsStudio 及对应 aiopsstudio 技术标识；运维工作台与分析入口语义 MUST 保持不变。

#### Scenario: 新安装及协议访问
- **WHEN** 使用新包、AIOPSSTUDIO 环境配置和新 Node/Provider agent 启动
- **THEN** 页面、API、协议和文档标识一致，只有受控兼容与历史记录可包含旧名

### Requirement: 有数据的旧部署兼容
系统 SHALL 在兼容路径识别完整旧数据库表、状态目录、Cookie 和部署归属，复用既有数据及原会话期限；系统 MUST 拒绝新旧配置冲突和不确定资产，不得因改名静默创建空历史或接管无关卷。

#### Scenario: 旧配置与新标识并存
- **WHEN** 提供原部署配置及已有完整存储
- **THEN** 新入口可访问原 owner 资源，凭据和主密钥不重置；冲突设置明确失败，数据不被复制或清空

#### Scenario: 自定义状态的数据库身份
- **WHEN** 仅用新键选择自定义状态目录
- **THEN** 无论目录首次或已存在，必须显式提供外部数据库 URL 或完整数据库资产身份；保留同值旧键时继续原资产选择，不静默切到新库

### Requirement: 发布与历史完整
系统 SHALL 保持同一仓库身份、Issue 编号/状态、MIT 和 Git 历史。安全门禁 MUST 保持启用，旧 profile 不能被更名静默忽略，新图必须复核且历史批准可追溯。

#### Scenario: 仓库更名后发布
- **WHEN** 仓库原位改名并推送新品牌代码与设计
- **THEN** 远端身份与历史保持，Issue 正文及当前链接更新，完整历史扫描和准确提交 CI 通过
