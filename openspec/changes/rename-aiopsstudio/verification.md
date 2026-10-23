# AIOpsStudio 更名与完整设计同步验证

## 变更与设计状态

- 同一公开项目由旧名原位更名为 AIOpsStudio；Python 包 `aiopsstudio`、发行包 `aiopsstudio-workbench`、Node 包 `aiopsstudio-web`、前后端 agent `aiopsstudio` 和环境前缀 `AIOPSSTUDIO_` 一致。MIT、Git 历史和仓库身份保留，不重建或改写历史。
- 完整阅读入口为 `docs/design/AIOpsStudio完整设计.md`，整合四份来源；第 2 章按已实现、部分实现/待验证、待开发区分，第 15 章完整保留后续工作。13 个工作包正文与路线图一致，91 条验收，阶段 5/4/3/1，E01–E21 全部保留；122 条本地引用和 21 个锚点通过。功能完成状态未因品牌或文档同步改变。
- 新旧环境值冲突拒绝；自定义状态仅用新键时要求显式数据库 URL 或完整资产身份，不能误选新空库。旧整套表、原主密钥、状态和有效 session 有界兼容；不自动迁移物理数据库/卷/文件路径。
- 当前公开树中的旧名仅位于集中兼容、只读安全 helper、对应合成测试和历史图像批准。历史提交内容保留原义。

## Mac 隔离验证

- 最终后端 156/156 通过；TypeScript/Vite 构建及固定上游 Node 回归通过。保留既有大分包提醒，未改变依赖版本或增加功能。新本机目录已离线重装相同锁定依赖，避免 editable 路径和脚本入口指向旧目录。
- 独立 PostgreSQL 16：7 组断言通过；新表、完整旧表复用、history/session/密文/事件/租约保留、修订和事件单调、混合表拒绝均验证。仅 loopback 和 tmpfs，无宿主挂载/持久卷，两个本次临时容器均清理。
- 隔离 API + 实际 Node：9 组断言通过；品牌标题、鉴权、新 Cookie、旧 Cookie 不延长 DB session、仅新 agent 注册、两次终态订阅不新增 run 或用量、owner binding 幂等及旧 agent 404。仅本次两个子进程和临时目录已清理。
- 安全合成 Git 回归 34/34 通过，覆盖新/旧 profile、同值/冲突/空值/多值、安装器不覆盖不同来源，以及只读新旧完整表组检测；没有放宽扫描规则。
- OpenSpec 全量严格检查 14/14 通过；主规格 `project-identity` 与本次 delta 一致。
- 上述验证没有真实模型或外部源调用；摘要见 [公开结构化记录](../../../docs/validation/2026-10-23-rebrand.json)。它们证明兼容与协议，不能代替 R01–R13 业务验收。

## 首败、修复与独立审查

- 新迁移测试首轮漏设 deploy 导入路径，修正测试环境后通过。安全测试两项首次因 macOS 临时路径规范化断言失败，改为比较规范路径后通过；旧 profile 原键保持断言保留。
- 临时 PostgreSQL 首次 Unix socket 过早就绪，在 Store 检查前宿主连接失败；改为 TCP 和有界连接等待后通过，首轮容器也已清理。
- 独立审查发现自定义旧状态改成同值双键后可能选择新数据库的 P2；修复旧资产选择，canonical 自定义目录必须显式 DB 身份，并补空/相对状态目录拒绝回归。最终 156 项通过；未留由本次差异引入的 P1/P2。
- 主规格首次严格检查提示 Purpose 太短，补充实际目的和边界后 14 项通过。所有首败没有改记为首次通过。

## 图片和公开安全

- 内置 imagegen 精确替换通用图中的产品品牌，保持布局与泛化标签；三份当前 PNG 使用新路径，其中两个 Chat 引用为相同字节。逐张目视及 macOS Vision OCR 无现场标识。
- 仅保留 IHDR/IDAT/IEND，逐块 CRC、无尾随数据；移除附加生成元数据。新图审核后登记精确哈希，旧路径/旧哈希仅保留供完整 Git 历史检查，不绕过二进制门禁。生成约束见[素材说明](../../../docs/design/assets/architecture-v1/说明与检查.md)。
- 当前副本安全 profile 配置键更新为 canonical；外部 profile 原字节不改，原实例和其他副本的 Git 设置不改。钩子保持启用。
- 14 项 Issue 的更新草稿逐字节通过暂存树规则、Gitleaks 和仓库外只读 profile 比对，88 项已知私有材料，无命中。

## GitHub 与发布进度

- 仓库原位更名为 `john-ops-lab/AIOpsStudio`，repository ID 不变，public/MIT/main 保持；本机公开目录和 origin 同步新名。
- 现有 14 项 Issue 全部读回核对：标题/正文与已扫描草稿一致，编号、ID、状态、labels、assignees、milestone 和评论数量保持；全部加入完整设计入口，旧品牌正文命中为 0。没有 PR 或 Discussion 需要迁移。
- 当前完整暂存树已通过现有通用规则、Gitleaks 与只读私有 profile 的 88 项材料比对，无命中；保留钩子。69 份 Markdown 的 137 条相对引用全部有效，当前跟踪文件名中无旧品牌。推送仍按实际提交完整祖先执行检查，实际推送钩子对包含实现提交在内的 13 个提交、431 个对象执行完整历史检查及同一私有比对，无命中。

- 实现提交 [4825c94](https://github.com/john-ops-lab/AIOpsStudio/commit/4825c9461d96eee8b77c3bae4876bbd206501c3f) 已正常推送到 main。远端分支 SHA 和完整 Git tree 与本机一致，完整设计正文逐字节一致。
- 该准确 SHA 的 [Publication security CI](https://github.com/john-ops-lab/AIOpsStudio/actions/runs/39931807282) 已成功；完整公开历史和 34 项合成回归通过。保留上游 runner/Node action 迁移提醒，不作为应用业务验收。
- 本验证与 tasks 在以上发布和 CI 后补记；记录提交按自己的 SHA 核对远端与[安全工作流](https://github.com/john-ops-lab/AIOpsStudio/actions/workflows/publication-security.yml)，不复用前一提交 CI。

## 本机运行入口

- 切换前只读一致快照与私有备份已准备；原配置、密钥、数据库和制品保持原值，旧物理路径用于兼容。
- Mac API/Web 已从新公共源码入口运行，原访问地址保持；仅替换经身份确认的两进程，没有使用会停止其他进程的整体 stop。
- 切换前完整比对通过且无待执行/运行任务、无启动清理候选；切换后 16/16 断言通过，数据库所有表/实体/schema、全部制品/附件、配置/密钥字节及数据库/卷/转发身份完全一致，worker 空闲且未变。原源码保留为回退副本。
- 只读匿名核对健康、首页新品牌及私有历史鉴权，没有新登录/session/audit、业务 run、模型或源调用。原 session 记录保持，旧 Cookie 协议升级由隔离 HTTP 验证支持；未宣称原浏览器免登录验收。
- 仓库外保留只切 API/Web 的启动及回滚入口，回滚仅语法核对、未执行。原物理数据/备份路径是兼容和恢复材料，不做无必要改名；私有原始收据与备份不发布。
