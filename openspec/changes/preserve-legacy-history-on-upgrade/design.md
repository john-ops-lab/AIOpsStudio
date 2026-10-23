# Explicit immutable archive
升级维护窗口中先生成清单，再用同一清单登记。登记时重读并核对全部候选对象，拒绝在途旧 run、混入可信新 run、跨 owner 关联和清单变化；仅新增 `legacy_archive/upgrade-v1`，已登记清单不允许扩容。应用启动不自动登记。

只读 API 使用显式 `allow_legacy_archive` 参数，默认严格。清单摘要与当前内容完全一致时才允许 owner 查看；列表/详情标记 `legacy_unverified`。旧会话详情不进行历史补写或附件修复。原生成附件仍校验文件大小及 SHA。普通上传保留原 owner 隔离，但旧档案会话禁止继续上传或把材料送进模型。

严格执行路径、history_context、重试、AGUI、模型上下文及补读不使用只读例外。旧档案不是已验证证据，不重建源 scope 或策略快照。继续分析须新建会话，不自动继承旧材料。清单文件属私有部署资料，不进入 Git。

## Offline operation

使用 `scripts/register_legacy_archive.py --plan <private-plan.json>` 生成权限为0600的清单，再用 `--register-plan <private-plan.json>` 登记。数据库连接仅从 `AIOPSSTUDIO_DATABASE_URL` 读取；清单必须在仓库外，不进入 Git。脚本不导入应用启动入口、不初始化账号或配置；仅增加清单记录。切换前先停止新提交、确认没有在途旧运行，维护人员核对备份、清单和原行/文件摘要。应用启动不自动迁移。

保留期清理跳过清单登记的运行；原会话详情不执行缺失助手消息/报告附件的自动修复，避免读取本身修改原记录。对其他 owner 的直接关联提交先返回404，再处理本 owner 档案的409提示，避免暴露他人档案存在性。
