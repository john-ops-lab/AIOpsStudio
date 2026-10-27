## Context

历史已按 owner 保存，附件已具备实际字节摘要、配额及报告关联。当前列表在应用层全量取出，缺少用户可控的归档、导出、删除和引用保护。本增量仅在隔离 Mac 开发验收，现有运行实例的配置、历史、附件不迁移或清理。

## Decisions

1. 数据库查询使用 Store.query_documents 的 owner/search/status/archive/time 条件和稳定 created_at/id 游标。游标由数据库层绑定过滤条件和首批上界。页内仍逐条执行最新材料授权；取不到完整可见页时继续消费数据库页，不向用户泄漏未授权记录数量。
2. GET /conversations 支持 search、archived、status、start/end、limit、cursor，返回 items/next_cursor。归档只改变可见分类，归档会话禁止新分析/上传；恢复不改原消息或版本。旧无 lifecycle_version 数据按版本1兼容读取，新写记录标版本2，无启动批量回写。
3. 导出仅包含当前 owner 已重新授权的会话、运行公开视图及附件实际字节/base64；排除服务器路径、配置快照、凭据引用和私有映射。导出总量有界，错误摘要或无权材料拒绝；原始证据不隐式全量导出。
4. 删除采用预览和确认两步。预览返回计数、阻断原因和过期 token；token 绑定 owner、目标类型/ID、完整引用集合与 revision 摘要。确认重新授权、检查在途任务和外部引用，并比对摘要；变化返回409。附件被保留消息/运行/报告引用时禁止单独删除。整会话删除可一并处理其内部终态引用，但外部存活引用或活跃任务必须拒绝。
5. 确认先提交最小 tombstone 与清理任务，再移除本服务计算出的受限文件。下载、列表、材料读取先拒绝 tombstone；失败清理保留 pending 状态，重试同 token 继续回收，不能因为进程中断复活记录。数据库和文件不假装原子事务；文件缺失按幂等回收处理，路径越界/符号链接拒绝。
6. 容量以未删除附件记录与受控磁盘文件对账，删除后不再占用户配额；保存失败删除新文件且不留成功 metadata。生成报告仍按 run 幂等，已保存但丢失的文件允许 repair 原 ID/摘要，报告内容不得改写。delivery_status 记录 ready/pending/failed，repair 只保存附件和关联，不派发模型或源。
7. Chat消息新增顶层 mode=analyze/explain/followup，保存在消息收据与提交 run 中，重试相同client_message_id沿用原mode。explain/followup需要同会话终态parent并始终新轮，explain不可新增附件；解释所需最新授权与禁来源工具由Runtime实施。analyze兼容已有活跃轮补充逻辑。归档/删除会话不接受新消息。
8. 崩溃遗留的无记录附件只在 owner 私有目录内预览/确认回收；创建文件与metadata、预览扫描和确认清理使用相同 Store.atomic 锁，正常在途文件不可被扫描回收。孤儿字节计入用户配额，回收token绑定owner和文件大小/mtime；未知文件名和符号链接不回收，受metadata引用的文件永远排除。
9. 显式legacy_archive绑定完整旧内容，保持原只读协议；可查看/下载/授权导出，归档/删除返回固定legacy_archive_readonly冲突。普通非legacy旧记录按缺省字段兼容读取，用户操作时才写lifecycle_version，不进行启动批量修改。
10. 一致备份格式与实际恢复由 verify-execution-recovery 消费本记录/字节/版本清单。Case共享引用和人工裁决不在本批，未知外部引用保守拒绝，不扩大owner权限。

## API

- POST /conversations/{id}/archive {archived,expected_revision} -> conversation。
- GET /conversations/{id}/export -> 有界JSON。
- POST /conversations/{id}/deletion-preview -> {preview_token,expires_at,counts,blocked,reasons}。
- POST /conversations/{id}/delete {preview_token} -> {id,deleted,already_deleted,cleanup_status}。
- 附件使用 /attachments/{id}/deletion-preview 与 /delete；GET /attachments/usage 返回真实记录/字节配额及孤儿/缺件对账。
- POST /attachments/orphans/deletion-preview 和 /attachments/orphans/delete 采用独立预览token回收本owner无记录临时文件。
- POST /runs/{id}/repair-report -> {delivery_status,report_attachment}。

## Validation

定向测试覆盖：两用户隔离、游标过滤绑定/稳定页、归档恢复与读写限制、导出实际字节与敏感字段排除、引用/活跃任务保护、预览过期/修改后确认拒绝、删除重复与旧下载拒绝、文件清理失败再试、配额回收/保存失败无孤儿、旧记录兼容、三个消息mode与独立制品修复。真实PG/浏览器/恢复矩阵由主集成串行执行，保留首败，不把QA补读称为模型自主补读。

## Astra 本地复审补修

分页暂时失败保留既有页并显式重试；权限失败清缓存。回收pending/不确定时不丢token，关闭动作受保护；删除完成清理staged，管理错误位于常驻通知区。
