# #10 执行中断与一致恢复验证

状态：生产代码已实现；合成与隔离 PostgreSQL 定向验证已完成，真实 MiniMax、完整进程中断和浏览器联合验收已由 M2 主集成执行，详见末尾统一记录。本文不将协议通过表述为业务或用户验收。

## 范围与隔离

- 使用 Mac 独立开发副本、临时 SQLite 和主集成提供的两个显式命名隔离 PostgreSQL 数据库；不对原运行实例注入故障。
- 本项 worker 未启动容器、API、Web 或真实模型，未写原配置、对话、附件；测试仅使用合成数据和 SDK MockTransport。实际 PostgreSQL 测试由主集成拥有其数据库生命周期。
- 不改冻结 M1 fixture、oracle 或允许字段。explain 的正例通过独立 M2 测试策略显式声明 history 字段；原冻结策略未声明时拒绝且零派发。

## 已核验断言

| 边界 | 证据 |
| --- | --- |
| 代次隔离 | 两个 Store 连接使旧租约失效，旧 run/invocation/event 写入拒绝；原用量保留，已发未回模型用量标 unknown |
| 一致提交 | 提交前过期回滚 metadata 与本轮发布文件；终态交付结束前不让其他 worker 抢占有效租约 |
| 数据库查询 | owner/search/id/status/archive/time offset/conversation 过滤、条件绑定游标、首批上界与旧空owner兼容 |
| 独立重试 | current配置变化后仍用原冻结快照；新轮独立预算和旧用量引用；客户端快照注入、范围变化、blocked拒绝 |
| 无源解释 | 实际 SDK 序列化但合成 transport；两次连续解释均无tools，来源/工具计数为零；原报告不改，原始引用可导航；环/跨会话/已删除拒绝 |
| 队列 | 所有测试purpose服从配置上限，拒绝新入队时不取消已有任务 |
| 完整备份 | 数据库、原文件字节、Fernet凭据、Skill实际发布包及引用、累计用量保留；不恢复有效会话或旧租约 |
| 失败关闭 | 错密钥、数据库/文件缺失、附件blob缺失、Skill参考缺件、非空权威表/checkpoint、并发新增、目录重叠、复制字节变化拒绝 |
| 恢复隔离 | restore_guard未核对时执行器及HTTP工作区拒绝，来源/模型零派发；只有身份和验证标志可访问 |
| 协议兼容 | 完整AG-UI定向覆盖原终态消息重试、重复message_id与变化runId；内部protocol身份保存在新轮，原owner边界不变 |
| Mac路径与旧档案 | 规范化路径别名可恢复；消息原文本不重写；严格核对源legacy摘要后绑定目标路径并留证明链，原档案只读且源篡改拒绝 |

## 运行记录与首败

1. 初始 recovery 定向首跑为9通过/1失败：explain试图使用冻结策略未注册的history字段。修复为显式M2正例授权，同时保留原策略零派发拒绝负例；不放宽正式策略。
2. 中间受影响集合曾为133通过/1跳过。后续补充重试、恢复负例与连续解释后，最终广覆盖定向为122通过/1跳过/1失败；唯一失败是既有 `test_model_tool_continuity.py::test_real_graph_and_sdk_keep_completed_tool_round_and_hide_reasoning[True]` 的自然报告timeout时序：期望3次SDK请求，实际2次，未返回执行错误。保留该首败，不修改断言，交主集成在统一负载下复查。
3. 连续解释测试首次补入时有测试装配失败：第二轮SDK被第一轮monkeypatch替代，导致客户端登记KeyError。修复测试的独立SDK构造后正负例通过，未改变生产判据。
4. M2全后端首跑暴露旧AG-UI终态消息重试409：retry在入队阶段额外解密凭据，与初次submit语义不一致。统一为入队查撤销metadata、派发前解密拒绝；同时修复protocol_message_id保留。没有改旧测试oracle。
5. 完整AG-UI、recovery、ownership定向运行：**48通过，6.11秒**。独立审查随后发现Mac路径别名及legacy档案摘要随重定位失效，修复后最终 `test_m2_recovery.py`：**28通过，1.76秒**。先前HTTP恢复隔离相关定向包含在120通过/1跳过集合；真实PG跳过仅因worker未提供显式隔离URL。
6. 主集成在fresh source/target隔离PostgreSQL运行 `test_m2_recovery_postgres.py`：**1通过**，覆盖真实checkpoint/blob/writes保存、旧代次拒绝、checkpoint非空目标、复制期间第二Store并发新增以及完整恢复。此项为主集成回报，不与worker的SQLite结果混同。

独立审查另外实际复现过附件blob缺失、恢复目标位于备份子目录、Skill已发布包缺件仍成功、Mac路径别名恢复拒绝、legacy档案摘要失配五个问题；已加入上述检查与合成回归。worker未保存这些问题的修前pytest运行，不把审查复现冒充其本地首败日志。

## 私有离线操作

`scripts/backup_workspace.py` 提供 `backup`、`verify`、`restore`、`authorize`。数据库URL只通过指定环境变量名读取，独立密钥文件与当前授权事实文件必须为0600或更严格；命令只输出摘要状态，不输出连接串、密钥、原配置、材料或私有manifest。

备份包含私有数据库与文件，应由操作员留在仓库外。HMAC绑定密钥摘要与一致集合；本功能没有额外打包加密，独立主密钥永不进入备份。恢复只接受与源/备份互不重叠的空目标；授权确认必须由操作员提供当前策略、撤销secret/user集合及确认者，旧备份不能证明远端账户当前有效。

## 未宣称覆盖

真实MiniMax业务正确性、浏览器接受度、跨进程kill/restart整链与原实例指纹保持由主集成最终记录；本项结果不代替这些验收。不提供自动续跑、多worker高可用、远端备份调度、自动撤权发现或原实例覆盖恢复。

## M2 主集成完成

统一最终验收与首败见 [M2 集成验收](../../../docs/validation/2026-10-27-m2.md)。本批提交一个集成 PR 供审查；不合并、不更新原实例，也不将隔离验收称为用户接受。
