# Proposal

> 本文件保留首次公开初始化的历史范围，已去除现场标识。后续公开副本与 Git 历史清理由 `sanitize-public-history` 跟踪；本记录不证明旧对象已从远端删除。

## Why

用户要求新建公共 GitHub 仓库 `john-ops-lab/AIOpsStudio`，采用 MIT 并同步代码、正式设计、OpenSpec 与公开验证摘要，使仓库可以独立阅读。

## What Changes

- 增加 MIT LICENSE，纳入正式原创设计和原型说明，修正仓库内链接。
- 同步 README、开发规则、规格映射和验证摘要，保留真实模型 `partial` 及首次失败。
- 对候选文件和拟发布历史进行已知凭据检查，实际值仅在内存参与比对。
- 创建公共仓库并核对主分支、公开属性、MIT 识别及关键文件。

## Capabilities

### New Capabilities

无。本变更仅涉及当时的公开打包、文档与远端同步。

### Modified Capabilities

无。`.openspec.yaml` 设置 `skip_specs: true`，当时主规格行为不变。

## Impact

影响许可、文档、设计副本、OpenSpec 发布记录与 GitHub 仓库；不迁移本机应用、数据库、用户会话、附件或凭据，不新增模型及源系统调用，不创建标签或 Release。后续部署参数和历史清理不从本次旧记录推断。
