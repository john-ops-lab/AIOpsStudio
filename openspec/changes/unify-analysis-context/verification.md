# R02 实现与验证

日期：2026-10-23。本文所列检查在 Mac 独立开发副本运行，使用临时存储、合成材料和 mocked HTTP；不连接原实例、真实源、真实数据库或模型。M1 其他真实调用由总验证记录独立说明。

## 已验证

- `backend/.venv/bin/python -m pytest backend/tests/test_analysis_context.py -q`：54 个合成边界案例通过。
- `openspec validate unify-analysis-context --strict`：通过。
- 覆盖跨环境/集群/namespace/type/id 主键、同名歧义、缺映射、同源 ID 冲突、显式键服务端验证、重建对象时间区间、未知历史有效期阻塞。
- 覆盖绝对 UTC/时区等价、窗口冲突、相对时间与过高精度拒绝、具名基线可比性未知、源偏差冻结及基线窗口再次核对有效期。
- 覆盖目录及定义版本、固定身份/窗口/查询冲突、JSON 字符串身份模板、未登记参数拒绝、无目标发现降级。
- 覆盖成功空结果/权限/失败/分页/陈旧/截断/越界、未知源时钟、未知或不支持历史拓扑、旧格式不自动升级。
- 运行时合成检查覆盖 needs_input 在源/模型 dispatch 前结束，以及原生参数由服务端冻结绑定、冲突不出站；API 合成检查覆盖新格式校验、旧格式保留、owner 和来源撤权。
- 独立复核补充显式返回身份、缺时间绑定的历史 topology、完整性/hash、片段原始下标与投影字段、当前出站策略和撤销凭据边界，详见 [review.md](review.md)。

## 最后一次定向检查

- Reader 与 connector 合成边界检查曾完成 69 项通过，覆盖完整响应包字节/用量、原始行坐标、不可变材料和实际 SDK 传输守卫。其后新增的独立复核也包含在下列定向 bundle 中。
- `backend/.venv/bin/python -m pytest backend/tests/test_analysis_context.py backend/tests/test_material_reading.py backend/tests/test_connector_authorization.py backend/tests/test_review_regressions.py backend/tests/test_runtime_trust.py backend/tests/test_trust_api.py -q`：159 passed、1 failed。唯一失败是新补的合成 `CorrectingModel._generate` 测试替身不接受当前 LangChain 传入的位置参数，尚未到材料错误恢复断言；10 项独立 review 回归在该轮全部通过。
- 集成负责人随后修正上述测试替身签名并报告该单例通过；未把“159 项通过 + 修后单例通过”改写为同一轮 160 项全部通过。冻结后的全量测试由 M1 总验证记录给出。
- 最后一次 `npm --prefix web run build` 通过（1.65s），包含最终结构化结论/模型原文标签及返回对象未知展示；保留现有超过 800 kB 的 chunk 提醒。该最后一版文案完成编译，未追加宣称新一轮浏览器业务验收。

## 首败与修复

补充具名窗口映射有效期检查时，首次测试收集发现括号缺失导致 SyntaxError；补齐括号后重跑全部 54 例通过。此前 41/52 例通过结果不替代最终结果。

## 前端合成验证

- 依照 React best-practices 与 frontend-testing-debugging，`npm --prefix web run build` 通过；保留现有依赖的大 chunk 提醒，没有为此扩大重构。
- Chrome 扩展可用，通过原生浏览器工具打开随机 loopback 上的独立合成 API/静态构建页面。没有访问现有运行实例，不连接模型或源。
- 页面身份、非空内容、框架错误覆盖层、最终控制台、桌面布局及交互均检查。最终浏览器会话无 error/warn；截图保留在仓库外。
- 报告按保存事实、结构化核对、潜在影响、观察、反证、未知与候选分组；模型原文明确为未核验。后续独立复核进一步收紧为“保存材料的确定性计算/结构化结论核对”，只有程序生成元组可展示为结构字段通过，model_text 始终单列未核验；返回身份未知不能归到查询目标。对象/窗口折叠面板显示服务端固定身份与具名窗口。
- 证据列表区分部分覆盖、成功空结果与权限拒绝；精确片段 GET 拒绝显示错误，POST 定向读取显示位置、anchor、保存完整性、用量；下一页复用原 selector 与 next_offset，不把保存未完读成源已查全。
- 模型设置新增查询目录及两类独立修订策略 JSON。未配置策略默认禁用草稿；合成 source 策略保存结果显示未启用。上下文表单已验证范围字段可编辑，补充窗口和完整键入口存在。
- 发现并修复：片段随后成功读取仍显示旧 anchor 错误；后续成功结果清除该旧错误。构建替换后未刷新的旧页面引用已删除 lazy chunk，首次导航失败，刷新最终构建恢复；未把该首败隐藏成首次通过。最终补充计算元数据时曾触发 TS5076（混用 `||` 与 `??`），加明确括号后完整 build 通过。
- 浏览器自动化填 datetime-local 返回超时，虽然原生 DOM 已有值，但未证明 React 状态收到该输入；因此不把该次操作算作具名窗口表单提交验收。窗口有效性已由模块单元验证，完整表单提交需集成复核。移动视口和真实后端策略、模型业务链路尚未由本次独立页面验证。
- 验证后关闭独立 fixture 服务，删除临时脚本、端口记录及构建日志；截图仅保留供本机审查，不进入公开资产清单。

## 尚未验证

真实源对象与响应身份规则的现场正确性、历史数据能力、真实模型对业务问题的完整性、移动视口及用户接受均不由上述合成检查证明。主规格已同步当前确定性契约；主流程保留真实 MiniMax 检查的 partial 边界，不以模型产生报告或 HTTP 成功代替业务验收。

## 最终独立全栈验收

- 使用 Mac 专属临时 PostgreSQL 数据库、0700 状态目录、独立 Fernet key 和合成管理员，启动随机 loopback 上的真实 FastAPI、Node/Web 和最终构建。Chrome 扩展完成真实登录、会话创建、运行、报告画布、片段及策略操作；未访问原运行实例。
- 初始绝对窗口由原生 Chrome 逐段输入，UI 保存后直接核对 PostgreSQL 中的 UTC 值。完整对象键、非空 cluster/namespace 及 baseline/change/validation 的绝对值经实际 API 预置，随后在真实 UI 回显、修改三种选取理由并保存；PostgreSQL 与固定上下文均一致，baseline comparability 保持 unknown。此项不写成所有具名日期都经键盘从空值创建。
- 未登记完整对象键的真实 Chat 进入 needs_input，模型/工具/MCP 请求全为 0、invocation 为空，刷新后保留窗口与澄清。首次页面仍错误显示 active 提示，按集成负责人授权将 context.ts、Runs.tsx 的 active 状态收紧为 queued/running；最终实际页面已无取消当前轮和正在分析提示，并说明继续追问开启新轮。
- 另一个 Chat 通过真实运行时完成：真实 SDK 配合 MockTransport 产生两次模型 HTTP、一条合成源调用、一份保存证据，model_generated=true、终态 partial。画布展示真实 0.18 ratio 的程序生成结构化元组、返回身份核验和模型原句未核验；证据显示声明范围 complete，未将其升级为整个分析完整。
- UI 对同一 run/evidence 读取投影片段；真实 API GET、PostgreSQL receipt.result、UI anchor 与数据一致，重复 POST anchor 稳定，原始 source_indices 与 projection_fields 正确。为单独验证精确引用入口，在私有 QA 报告中加入明确标注“服务端种入、非模型新结论”的引用示例，保留原模型报告；点击后实际读取同一 fragment，不宣称模型自动产生该引用。
- frozen 单 run fixture 未登记 Chat 的空 incident_id 路径，首轮被出口拒绝且无模型/源调用；通过真实管理 UI 新建仅增加 /inputs/incident_id 精确允许项的 QA 策略修订后完成上述流程，未改生产边界或冻结 fixture。之后在真实 UI 禁用来源授权，run/evidence/conversation/fragment/material-read 五条实际 API 均返回不泄露存在性的 404；页面重载显示拒绝，原值与锚点不再可见。首次验收脚本假定 403，保留该 oracle 错误，不当作产品错误。
- 最终 build 通过（1.59s），保留现有 chunk 提醒；最终 Chrome 控制台 warn/error 为 0，桌面截图已目视，截图、实际 ID、原始收据与详细 checks 仅保留在仓库外私有临时目录，不作为公开资产。该流程使用合成模型/源，属于真实 API/存储/UI 集成验收，仍不等于真实业务验收。

## PR 15 functional review correction

The first in-memory reproduction returned `complete`, `pagination_complete=true`, and no reasons for a successful Prometheus payload explicitly carrying `coverage.status=partial` and `pagination_complete=false`. Runtime inference now preserves explicit limiting states, incomplete pagination, continuation and truncation; malformed result rows/timestamps degrade to unknown. The regression crosses into deterministic grounding and verifies that such a claim remains unverified.

Mac local targeted check: `pytest -q backend/tests/test_pr15_coverage.py backend/tests/test_analysis_context.py backend/tests/test_grounded_reporting.py` — 111 passed. This is synthetic protocol/grounding verification, with no model or source network call and no existing application state opened. Final aggregate validation is recorded by the integration review.
