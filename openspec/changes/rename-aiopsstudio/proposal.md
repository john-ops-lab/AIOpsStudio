# Proposal

## Why

用户要求项目、代码、设计、GitHub 仓库及 Issues 全量更名为 AIOpsStudio，同时保持现有用户数据和配置可用。仅改页面标题会留下包、协议、部署与文档不一致。

## What Changes

- 产品名、Python 包/构建元数据、前端/Node agent、默认环境与部署资产统一新标识。
- 已有环境变量、Cookie、数据库表、状态目录、Docker 归属和安全 profile 仅通过明确的集中兼容路径识别，禁止因更名建空库或丢弃私有扫描。
- 更新设计文件名、文本、锁文件、通用图片及所有当前链接；保留历史 Git 对象与必要旧图批准，不重写历史。
- 原位重命名同一 GitHub 仓库，更新本地 origin 和现有 Issue 标题/正文，保留编号、状态、依赖和 MIT。
- 核对本机已有实例，准备保留配置/数据的迁移与回退；只在 Mac 验证，不改变外部源。

## Capabilities

### New Capabilities

- `project-identity`：统一品牌与技术标识、旧部署识别及安全兼容约束。

### Modified Capabilities

无。运维工作台、分析、owner 隔离和只读工具边界继续有效，既有内部 investigation 业务枚举不改。

## Impact

应用包、页面、部署入口、测试、安全脚本、设计/OpenSpec、图片、GitHub 元数据和 Issues。新配置采用新名；不修改源系统、不更换模型、不重建数据库或移除历史。私有配置、真实运行材料不发布。
