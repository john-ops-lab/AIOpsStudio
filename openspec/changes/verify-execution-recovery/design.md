## Context

现有租约只约束领取/续租，普通保存和事件写入未绑定领取代次；失联 worker 在回包后仍可能覆盖终态。现有 Store 列表全量取出，不满足 M2 owner/搜索/分页契约。恢复不得自动重发用量未知的模型请求。

## Decisions

1. worker_slot 增加单调 generation（幂等迁移），每次领取给 run 写 lease_generation。执行 ContextVar 绑定 store/run/owner/generation；所有事务内变更、实际派发及响应接受在同一数据库锁下检查租约代次与有效期，提交前复核。PostgreSQL checkpoint saver 使用相同事务 advisory lock，独立连接在写前/提交前核对代次。文件采用临时文件、fsync、复核后发布，事务回滚回收本轮文件。终态报告交付结束前保留原租约；过期执行标 execution_interrupted 后要求显式新轮，不重放用量未知请求。未取得租约的合成单元测试直接执行保持兼容。
2. Store 提供 SQL owner/search/status/archive/time/conversation/parent 过滤和 created_at/id 稳定降序游标，游标绑定查询条件和首批上界。只增加索引和迁移记录，不改旧业务行；支持 SQLite 测试与 PostgreSQL。
3. explain 为 investigation 的独立 mode，必须有已授权同会话终态 parent，使用重新投影的保存报告/证据，不发现 MCP 或调用来源工具。连续解释保留 immediate parent 关联，引用与导航解析到已重新授权的原始 reference_run_id；拒绝跨会话、环、已删除或超过32层的引用链，模型响应后再次授权。followup 由会话桥接始终新轮。模型超时、预算和未知用量沿用 M1 边界。解释所需 history 字段必须显式注册到出站策略，冻结 M1 policy 不被放宽。
4. read_saved_material 显式枚举 evidence/skill_reference，selector 提供 pointer/offset/limit/fields/series/start/end 等已支持字段与中文样例，失败仍受控返回；不扩大出站字段规则。
5. 备份是离线、资源有界操作，先拒绝活跃执行；数据库一致点包含 documents/events/worker_slot 及四个 PostgreSQL checkpoint 表，原制品字节、Skill 固定版本清单与密钥摘要由 HMAC 绑定，独立密钥不写备份。默认256MiB、硬上限1GiB，并保留64MiB空闲。逐文件流式读取、拒绝符号链接和复制期间变化，校验 evidence/material_read 显式路径及附件 owner/id 派生 blob 引用；所有声明 artifact_path 的已发布 Skill 版本（含 experimental）还按快照正文/参考文本核对完整文件集合与字节，旧无包路径版本保持兼容。恢复在任何 mkdir/copy 前拒绝与源制品及备份目录相等或祖先/子目录重叠；目标数据库仅允许空schema与migration元数据，checkpoint数据和worker slot亦必须为空。复制后在同一事务锁内、任何DELETE前重新核对空目标和文件集合/字节，不覆盖并发新增记录。只重定位声明的 path/artifact_dir/artifact_path 字段，不修改消息文本。恢复不保留有效会话/执行租约，设置恢复隔离门；HTTP全表面及执行器在操作员提交当前策略/撤权事实前拒绝操作。远端账户有效性不从旧备份推断。
6. 报告制品失败与分析终态分开；生命周期模块完成 delivery_status/repair-report，当前执行scope内的 finish_run 受同一代次约束，独立修复仅终态且不派发模型。
7. 显式 retry 是新的 attempt，服务端私有 frozen_from 深拷贝原 config_snapshot、analysis_context、policy_snapshot 和 budget；客户端不能注入快照或变更原目标/窗口/模型。重新校验最新启用、blocked、密钥撤销与 frozen/latest policy 交集。新 attempt 独立计量并保存 retry_of/retry_usage_reference/retry_budget_semantics，原配置/报告/累计用量不变。followup 才使用新选择配置。
8. queue_limit 真实参与所有 purpose 入队，调低只限制新增，不取消已入队。管理草稿仅在显式 test 选择下允许 disabled/unpublished 候选；blocked、凭据、策略和资源预算照常拒绝。管理测试 quality_binding 由服务端按实际采用快照生成，普通分析拒绝 experimental Skill。
9. retry 入队与初次 submit 一致，仅根据已有凭据metadata拒绝已撤销项，不在入队阶段解密。缺失或无法解密的凭据由派发前 guard 拒绝，实际请求为零。内部 AG-UI protocol_message_id 固定到新 run 的 client_message_id，协议 runId 变化仍按用户消息身份重放原收据；公共重试 HTTP 不能注入该内部字段。
10. 恢复路径按声明字段的规范化源根相对位置映射，兼容 Mac 的路径别名。legacy_archive对完整记录的摘要先按签名源行严格核验，再仅为路径重定位后的目标重新绑定entry/plan摘要，保存原摘要与备份签名证明链；不改通用archived校验、不解除只读、不写原源。源档案已篡改时恢复事务拒绝。

## Validation

覆盖两个 Store 连接的过期/旧代次写入、在途源响应落盘拒绝、未知模型取消、队列设置、无源解释、连续解释、SQL分页/owner/游标条件绑定、显式重试快照与预算、备份正确恢复和缺件/密钥错误/非空目标/并发新增/目录重叠负例。真实 PostgreSQL、MiniMax及进程中断联合验收由主集成在隔离Mac执行。原实例只读快照验证不变；不对原实例注入故障。测试记录见 verification.md，合成协议通过不等于真实模型或业务验收。
