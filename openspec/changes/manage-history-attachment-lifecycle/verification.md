# 历史与附件生命周期分项验证

## 范围

本项为 M2 集成 PR 中的 #9，并提供 #7 消息模式桥接和 #10 报告交付/检查点回收接点。实现及测试均在 Mac 独立开发副本，使用临时 SQLite、合成用户/记录/文件；未启动额外服务或容器、未调用真实模型或来源、未修改原实例配置/历史/附件。

## 定向检查

命令：

```sh
backend/.venv/bin/pytest -q backend/tests/test_m2_lifecycle.py backend/tests/test_chat_services.py backend/tests/test_legacy_archive.py
openspec validate manage-history-attachment-lifecycle --strict --no-interactive
openspec validate history-lifecycle --type spec --strict --no-interactive
```

最终定向结果为 **35 passed**：新增生命周期17项、既有Chat14项、旧档案4项。change与主规格严格校验通过，`git diff --check`通过。此计数不替代全量集成、PostgreSQL、恢复、浏览器或真实模型验收。

- SQL owner/search/archive/time/cursor过滤与固定上界分页，跨用户或不同过滤复用游标返回422；相邻页没有重复或遗漏，分页后新增记录不混入原快照。
- 归档/恢复要求修订号并保留原消息/上下文；归档期间不能发送、修改或上传，其他owner拒绝。普通旧版本记录读取不回写，显式操作才登记lifecycle_version。
- 导出附件实际字节及SHA一致，跨用户、摘要损坏和有/无附件的导出超限均拒绝；运行快照、服务器路径、凭据引用不进入导出。
- 未引用附件经预览确认后回收配额，旧metadata/下载入口均404；token不属于用户、过期、记录变化或新增引用时拒绝。运行中及旧thread-only运行均阻止删除；外部引用仅返回固定类别，不泄漏其他owner标识。
- 整会话删除先写最小tombstone和可重试清理任务，再回收附件/原证据/固定Skill及准确checkpoint线程；数据库派生调用/阅读正文清除，旧run列表/概览/下载不再展示，原创建幂等请求不能复活已删会话。文件清理失败保持pending、访问立即撤回，同token重试可完成。
- 无记录临时文件计入该owner配额；只回收受控私有目录内候选文件，其他owner/已关联文件保留。实际并发上传在文件发布但metadata未提交时，请求孤儿预览被同一事务锁阻塞，提交后不再列为孤儿。
- 报告缺件修复保留原ID和字节/摘要，记录delivery_status且模型/来源调用计数不变；内容冲突不覆盖。临时写入、fsync、fence校验及独占发布位于事务内，COMMIT前失租回滚会移除刚发布文件，不留metadata或孤儿。
- explain/followup保留同会话终态父轮并创建新轮，消息mode幂等；解释新附件、无效mode及跨会话父轮拒绝。真正禁来源与模型结果由Runtime后续联合验收，本项不将仅提交收据称为真实解释通过。
- 已注册legacy_archive原内容和字节保持只读，可授权导出/查看/下载；归档/删除固定409 `legacy_archive_readonly`，不改旧完整摘要或自动转换权限。

## 首败及修正

1. 新增9项首次为2 failed/7 passed：游标校验的ValueError未转HTTP422，归档服务仅写archived而数据库过滤archived_at，导致已归档记录仍出现在普通列表。补齐HTTP映射及归档字段后，连同既有Chat为23/23通过，未改变断言或放宽授权。
2. 管理模块强制质量发布准入落盘时，既有Chat fixture直接发布导致27个setup errors、4个旧档案测试通过。经管理模块提供明确的合成“已发布旧版本”fixture替换原准备步骤后继续测试，未放宽生产发布gate或伪造评价通过。
3. 孤儿对账首次将已有关联附件路径做resolve，遇到既有“符号链接+超配额”拒绝测试时，提前返回404而非预期413，结果1 failed/31 passed。改为按服务端计算的关联文件名排除，不解析/读取已知symlink；原下载仍拒绝，配额断言不变。修复后32项通过。
4. 随后补充真正并发上传、旧thread-only任务和删除后派生读取检查，最终35项通过。阶段结果均保留其含义，后来的通过不覆盖首败。

## 主集成补充验收（已完成，见统一记录）

- 在隔离PostgreSQL执行稳定分页、删除/checkpoint/事务fencing及备份恢复联合检查。
- 浏览器操作搜索/归档/导出/删除预览/容量提示及报告独立修复，刷新/撤权状态核对。
- MiniMax自主正确补读、解释禁源、补查新轮按 #6/#7/#10 各自矩阵核验；不以QA操作代替模型自主能力。
- Case共享引用与共享恢复留待R13；当前旧档案保持只读，不宣称已经转为可编辑数据。原实例迁移/部署及用户接受不在本分项测试中自动完成。

## M2 主集成完成

统一最终验收与首败见 [M2 集成验收](../../../docs/validation/2026-10-27-m2.md)。本批提交一个集成 PR 供审查；不合并、不更新原实例，也不将隔离验收称为用户接受。

## Astra 本地复审

修复和最新验证见 [OCR 复审记录](../../../docs/validation/2026-10-27-m2-ocr.md)。原 MiniMax 样本及初版测试计数保留其历史含义，不冒充本轮重新执行。
