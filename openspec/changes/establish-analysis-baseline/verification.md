# R01 验证记录

日期：2026-10-23。执行在 Mac 本机隔离工作区，每次使用独立临时 SQLite/制品和固定合成源。默认合成模型不联网；真实模型仅引用主任务准备的仓库外私有 MiniMax 配置。本项未访问原实例、生产数据库或真实业务源，未更改现有本机配置/运行，也未提交或推送。

## 检查与冻结

- 固定原始源码：`bd9a949cff85e2f5b00b5dfd43d323909bfd0a1f`，由 Git archive 提取；新增评估器独立于被测应用包。
- `backend/.venv/bin/python -m pytest backend/tests/test_evaluation.py -q`：12 passed。覆盖输入/oracle 分离、实际工具返回影响报告、oracle 改变只改变独立评分、版本/源篡改拒绝、未知/重大失败分母、引用来源、调用列表逆序评分一致、文件/网络隔离、外部配置权限、固定字段策略未知拒绝和工作树复制。
- `openspec validate establish-analysis-baseline --strict`：通过。
- `openspec validate analysis-evaluation --type spec --strict`：通过；change 与 main spec 使用同名 capability。
- `git diff --check`：通过。仓库全部代码、文档、历史及外发安全检查由主任务统一执行，本记录不代替该门槛。
- 测量时先复制同一份应用代码供全部尝试使用。最终候选应用代码摘要为 `0a7b426a16ecc27723e98bacaaef0d37729eaba445535473811ad990d5473a94`，在操作员边界收口后单独完整重验，复用原始固定基线；当前应用代码摘要核对一致，较早候选、重试及首败全部保留在仓库外。

## 合成结果

| 固定套件 | 被测应用 | 严格案例 | passed | failed | unknown | 断言总数 |
| --- | --- | --- | --- | --- | --- | --- |
| legacy 15 案例 | bd9a949 | 8/15 | 78 | 4 | 6 | 88 |
| M1 18 案例 | bd9a949 | 7/18 | 83 | 16 | 6 | 105 |
| M1 18 案例 | 最终冻结候选 | 15/18 | 102 | 0 | 3 | 105 |

legacy 全部 15 案例各运行两次，评分 15/15 一致。M1 初次最终 schema 的旧版与候选各运行 18 案例 × 2 次，双方评分各 18/18 一致；之后应用补齐操作员边界，复用同一基线完整重验最终候选一次。最终可比性检查没有不明源/版本，重大退化为零。第一尝试定义总分，重复尝试不择优覆盖首败；不同套件与适配器之间不能直接比较分数。

M1 的剩余三项 unknown 为共享撤权、人工裁决和一致恢复；缺少实际操作收据，仍在严格分母中，不能称为 18/18。M1 保留真实 ChatOpenAI SDK、Deep Agents 和应用 HTTP request guard，只使用离线 HTTP MockTransport 生成合成响应。参考正文必须先取得实际补读，不能因后续收据倒推此前发送合法；未知策略字段仍拒绝。

公开结果为 [legacy 摘要](../../../evaluation/baselines/bd9a949-synthetic-v1.json) 和 [M1 同套件摘要](../../../evaluation/baselines/m1-trusted-synthetic-v1.json)。完整输入、实际请求、run/配置/证据 ID、私有收据、日志与原始观察仅保留在仓库外。

## 首败与修正

冻结前完整测量暴露两个评估器问题：模型调用无用量收据时不能照抄 Runtime 初始零值；源失败断言不能依赖 Store.list 顺序。补充回归后冻结 legacy，早期结果保留。这些是评估器修正，不计为应用改善。

M1 第一轮候选严格 14/18，101 passed、1 failed、3 unknown：固定 reading schema 缺少两个已经声明的窗口 `reason` 元数据字段，守卫正确拒绝真实补读交付。手审补齐已知字段后重新冻结，使用同一最终套件完整测量旧版与候选，未改源快照、oracle 或放宽生产守卫。首轮原始观察保留，不用最终结果覆盖。

## 真实 MiniMax 的有限结果

本项只执行三次 `normal-alert` 真实 MiniMax + 固定合成源尝试，没有自动 fallback、补造源数据或全矩阵真实模型重试：

| 尝试 | 结果 | 实际模型 HTTP / 守卫次数 | 实际输入/输出 token | 限制 |
| --- | --- | --- | --- | --- |
| 1 | failed | 1 / 1 | unknown / unknown | 显式兼容接口路径返回 404，原配置和首败保留 |
| 2 | partial | 1 / 1 | 1710 / 275 | 模型调用工具并请求参考资料；固定参考文本 schema 缺项，交付被拒绝 |
| 3 | partial | 2 / 2 | 3990 / 428 | 固定源无所请求工具/页快照，且模型选择器无效，保留缺口 |

这些请求通过实际 HTTP 守卫后才捕获 body；配置、认证头和 provider 原始错误未复制到公开结果。没有冻结价格表，费用保持 unknown。详见 [有限模型记录](../../../evaluation/baselines/minimax-synthetic-source-check.json)。后续集成中独立模型协议往返成功，或独立 PostgreSQL 合成回放成功，均由总验证另记，不能替代这三次 failed/partial 业务尝试或一致恢复案例的缺失收据。

本轮未连接真实业务源或重新验收真实案例。合成严格分数衡量固定取证、阅读与边界断言，不能当作真实根因准确率。后续经授权真实材料可按同可见输入、独立 oracle 和源/版本合同适配，现有 `score(observation, oracle)` 可复用。
