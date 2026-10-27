# execution-recovery Specification

## Purpose

在单执行槽与明确资源预算内隔离过期执行器，保留中断状态与真实累计用量，通过显式新轮重试、无源结果解释及私有一致备份恢复，保护已有配置、对话、附件、Skill 固定版本和最新权限边界。

## Requirements

### Requirement: 执行代次与副作用一致提交
系统 SHALL 为每次领取保存单调租约代次，并在派发、在途响应接受、事件、终态、制品及 PostgreSQL checkpoint 写入前和提交前核对相同有效代次。旧 worker MUST NOT 覆盖新代次结果，失败事务 MUST 回滚 metadata 及本轮新发布文件。

#### Scenario: 过期 worker 返回
- **WHEN** 旧 worker 租约已过期或被下一代领取且收到模型或来源回包
- **THEN** 旧回包、事件、终态及 checkpoint 写入被拒绝，不改写新状态；原实际用量保持，未能核对的模型用量明确 unknown

#### Scenario: 提交前过期
- **WHEN** 本轮写入期间租约过期
- **THEN** 数据库事务回滚，本轮新文件被回收，不留下成功交付记录

### Requirement: 中断终态与独立显式重试
系统 SHALL 将过期执行明确结束为 execution_interrupted，不自动重放已派发且用量未知的请求。显式 retry SHALL 创建新 attempt，冻结原配置、分析上下文、策略和预算，重新验证最新启用与撤权；新 attempt 使用独立预算并保存旧用量关联，原报告与累计用量 MUST 不变。

#### Scenario: 配置变化后重试
- **WHEN** 原 attempt 结束后 current 配置变化且用户显式重试
- **THEN** 新 attempt 沿用原冻结快照并标明 new_attempt_independent_budget，原用量不清零；最新 blocked 或撤权仍阻止执行

### Requirement: 有界队列与版本化无损迁移
系统 SHALL 对所有 purpose 使用 settings.queue_limit 和既有资源预算。调低上限 MUST 仅限制新增任务，不取消已入队；迁移 SHALL 版本化、幂等且不改写旧业务内容。Store SHALL 在数据库内执行 owner/search/status/time/conversation 过滤和稳定 created_at/id 游标，游标绑定查询条件并排除首批上界后的新增记录。

#### Scenario: 修改队列设置
- **WHEN** 管理员调低 queue_limit 而队列已有任务
- **THEN** 已入队记录保留，超出新上限的后续提交明确拒绝

### Requirement: 保存结果解释与原证据关联
系统 SHALL 将 explain 创建为有独立预算的新轮，只解释已保存且重新授权的同会话终态报告，不构造工具或发现/查询来源。连续解释 SHALL 保留 immediate parent 关系，并将引用和导航解析到重新授权的原始 reference_run_id；环、跨会话、缺件或已删除引用必须拒绝。

#### Scenario: 再次解释
- **WHEN** 用户解释前一条解释结果
- **THEN** 新轮关联前一解释轮，证据引用仍指向原分析，不生成新事实；来源与工具调用为零

### Requirement: 私有完整备份与空目标恢复
系统 SHALL 在无活跃执行的一致事务边界备份权威表、完整 checkpoint、原制品、附件、Skill 固定版本及密钥摘要，并校验数据库、文件和逻辑引用。独立密钥 MUST 不进入备份。恢复 SHALL 仅允许空隔离目标，任何删除前在同一事务锁内重新确认所有权威表、checkpoint 数据及 worker slot 为空。

#### Scenario: 完整恢复
- **WHEN** 操作员提供正确独立密钥及完整备份到空隔离目标
- **THEN** 原文件字节、配置版本、累计用量和 checkpoint 可核对恢复，旧有效会话与租约不恢复，原实例保持不变

#### Scenario: 终态后仍持有交付租约
- **WHEN** run 已标记终态，但 worker slot 尚未释放，或租约已过期而原执行尚未明确收尾
- **THEN** 备份拒绝，不清理原租约、不自动重放；待原执行流程明确结束并释放槽后才允许一致备份

#### Scenario: 独立主密钥位于制品集合
- **WHEN** 主密钥文件的规范化位置属于制品根，或实际制品内容含相同明文主密钥
- **THEN** 备份拒绝且不留下完成包，不复制密钥或将密钥写入错误/日志；外部独立密钥可正常使用

#### Scenario: 数据库业务记录含当前主密钥
- **WHEN** 会话消息等数据库内容含有同一明文主密钥
- **THEN** 备份在写入 database.json 前拒绝并清除本次失败包，错误不含密钥或路径；其他正常业务内容与不同文本仍按原值备份

#### Scenario: 缺件或并发目标变化
- **WHEN** blob、数据库或 Skill 声明包缺失、摘要/密钥错误、目标非空、复制期间并发新增记录，或目标与源/备份目录重叠
- **THEN** 恢复拒绝并保留原目标和原备份，不以清空数据完成恢复

#### Scenario: 路径别名与旧档案迁移
- **WHEN** 合法文件路径使用源根别名，或只读旧档案摘要包含需要重定位的路径
- **THEN** 恢复按规范化相对路径映射，仅在严格核对源档案摘要后绑定目标摘要并保存证明链；用户文本和原源保持，目标旧档案仍只读，源篡改拒绝

#### Scenario: 业务数据使用同名路径字段
- **WHEN** 用户输入、报告、历史、补读结果或未知文档含有名为 path、artifact_dir 或 artifact_path 的业务字段
- **THEN** 恢复逐值保留这些业务内容，只按文档类型和固定 schema 位置重定位服务端文件定位字段，不按字段名递归改写数据

### Requirement: 恢复后最新授权隔离
系统 SHALL 在恢复后设置授权隔离，在操作员显式提交当前来源/出站策略及撤权事实前拒绝模型、来源和工作区操作，不从备份推断最新权限。HTTP MAY 仅提供身份会话和不含备份细节的恢复状态。

#### Scenario: 尚未核对最新撤权
- **WHEN** 恢复实例未确认当前授权而用户访问工作区或请求真实调用
- **THEN** 服务明确拒绝且实际派发为零，旧历史、附件和数据库内容不被后台清理或修改
