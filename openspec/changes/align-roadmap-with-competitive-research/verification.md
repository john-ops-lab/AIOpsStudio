# 路线图研究缺口修订验证

本记录仅验证规划、设计与现有 Issue 的同步，不代表产品功能已实现或业务验收通过。

## 范围与责任

- 保留一个总路线图和十三个实施项，不新增 Issue；原有目标、交付及验收保留，增加研究缺口与优先级。
- R07 从 M2 前移 M1，阶段分布调整为 5/4/3/1；数据源范围与模型数据出站成为 M1 退出和不同权限团队使用的前置条件。
- 固定回放与发布质量归 R01/R05；受限 references 归 R03/R05；费用展示归 R01/R06；人工裁决归 R12；Case 显式共享归 R13；queue_limit 归 R05；备份清单与一致恢复归 R08/R09。
- R05 不等待全部未来场景；R09 不等待尚未交付的 Case，共享/人工裁决入口及 Case 恢复在 R13/R12 就绪后另行联合验收；R13 不强制等待全部工单/RCA 扩展。

## 验证进度

- 独立读者审查完成，无未关闭 P1/P2；修复 Case 恢复反向等待、Issue 相对链接和阶段编号链接问题。
- 十三个工作包的交付及验收逐项匹配准确 Issue 草稿，保留所有原验收条目；阶段为 5/4/3/1，硬依赖无环。
- 63 个修改文档内的相对链接目标存在，Issue 仅使用正确的 GitHub 绝对链接；git diff --check 通过。
- OpenSpec 严格校验：11/11 通过。本次为规划 change，未修改当前应用主规格，后续功能须另建 delta specs。
- 准确的十四项 Issue 标题/正文在隔离暂存树通过现有完整安全规则及 Gitleaks；仓库外私有 profile 启用，88 项已知私有材料比对，无命中。没有新增图片或素材。
- 写前再次读取十四项，确认未发生并发编辑；仅更新标题/正文。写后 14/14 的准确正文和标题与复核稿一致，state/labels/assignees/milestone/locked/comments 保持原状，仍全部 open、验收未勾选。
- 实际完整暂存树通过现有安全门禁及仓库外私有比对，无命中；已审查暂存差异，11 个文件均为设计/状态或本次 OpenSpec 规划记录，无应用代码、主规格及图片变更。
- 文档提交 [11665a5](https://github.com/john-ops-lab/AIOpsStudio/commit/11665a50e7c65a745ded982b26337f4761e38ce8) 已正常推送到 main，远端主分支 SHA 一致；11/11 修改文件的远端 Git blob 与本地提交逐项一致。
- 提交钩子检查完整暂存树，推送钩子检查实际目标及全部祖先；完整历史扫描通过，仓库外私有 profile 启用，未绕过门禁。
- 该准确提交的 [Publication security CI](https://github.com/john-ops-lab/AIOpsStudio/actions/runs/39919962687) 成功；完整公开历史及合成 Git 回归均通过。
- 本验证记录在上述发布与 CI 完成后补充；记录提交自身应按其 SHA 查看[安全工作流](https://github.com/john-ops-lab/AIOpsStudio/actions/workflows/publication-security.yml)，不复用前一提交结果。
- 最后对照初始提交，变更仅涉及 11 份设计/状态/OpenSpec 规划文件；应用源码、部署入口、脚本、测试、主规格及图片没有差异。

## 实现边界

本次只更改设计和 OpenSpec 规划记录及 GitHub Issue 标题/正文。应用源码、依赖、主规格和图片不变；没有启动应用、调用真实模型/源系统、执行迁移或故障注入，没有修改原运行实例、数据库或私有配置。后续实施分别建立行为 delta specs，模型验证优先 MiniMax，运行与测试仍在 Mac。
