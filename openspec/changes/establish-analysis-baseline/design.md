# R01 评估合同

## 边界与目录

- `evaluation/inputs/*.json`：Agent 可见的固定目标、绝对时间窗、用户材料、合成源快照、冻结配置和模型行为。每个文件独立可审阅，禁止 `expected`/oracle 字段。
- `evaluation/oracles/*.json`：评分器私有输入，包括事实断言、允许结论等级、禁止因果、引用目标、必须呈现的缺口及未来能力断言。加载发生在执行进程结束之后，执行适配器不接收路径或内容。
- `evaluation/suite.json`：案例清单、评分版本、基线完整 SHA、冻结内容摘要、适用 R02–R13 工作包。未来案例不可从分母隐式排除。
- `evaluation/m1/`：单独冻结的 18 案例/105 断言套件，加入缺失/歧义映射和实际请求 Skill reference。legacy 的 15 案例/88 断言不改写；不同套件或模型适配器的分数不能直接比较。
- `backend/aiopsstudio/evaluation/`：`contracts.py`、`scoring.py`、`replay.py`。评分器只消费序列化的实际观察结果与 oracle，不使用模型判断；重放器不导入评分器或 oracle。
- `scripts/evaluate_analysis.py`：负责父进程编排。每次 attempt 建立空临时 SQLite 和制品目录，通过真实 `create_app(testing=True)`、登录、POST `/api/runs`、GET 终态和证据接口回放；真实 Deep Agents 与运行队列保留，仅模型和源适配器可替换。`--baseline-ref` 只允许固定 `bd9a949`，`--candidate-ref` 可选择 Git ref 或工作树；工作树先复制并计算代码摘要，所有案例使用同一份副本。不安装或启动常驻服务。

## 输入与执行合同

`ReplayInput` 至少含 `id`、`category`、`work_packages`、`request`、`versions`、`source`、`model` 和 `checks`。版本冻结项为 model、Skill、tool_schema、summarizer、query_catalog、privacy_policy；对应内容按规范 JSON 的 SHA-256 核对。冻结项与实际快照不一致时整次运行失败，不能继续评分为通过。

合成模型是确定性工具使用适配器：从实际上下文的工具目录发起固定只读源查询，再根据实际收到的证据阅读视图形成报告。允许一个所有案例共用的简单保守策略；不得按 case id 查答案，不接收预设报告文字，不读取输入中未被工具披露的未来事实。可替换模型适配器必须保留实际请求/响应观测与版本身份；真实测量优先 MiniMax，仅经显式外部配置启用。

执行材料不含 `id`、分类、工作包、oracle 路径或内容；固定源快照只交给源适配器，不能交给合成模型。执行子进程通过文件审计拒绝 oracle 路径及符号链接绕过；默认禁止网络。父进程在每个案例执行结束后才加载并校验其 oracle。子进程环境仅保留执行必需项，不继承应用、provider 或 tracing 凭据。

legacy 使用保守 `BaseChatModel` 适配器；M1 使用 `synthetic-wire-extractor-v2`，保留真实 ChatOpenAI SDK 与应用 request hooks，只把 HTTP transport 替换为 MockTransport。模型策略读取实际 HTTP 消息和工具 schema，依照实际分页提示、保存数量与 reference manifest 发起后续调用。实际出站 body 在原守卫之后捕获，不能用 LangChain callback 代替。固定策略逐类型/字段登记，不能根据当次 HTTP 动态允许未知字段；缺映射须在任何模型/源请求前停止。

M1 固定 `application_settings` 和独立策略文档；查询目录明确身份/时间绑定、单位及返回对象身份合约。目标/时间参数由应用绑定，合成模型只传实际暴露的可变参数。资料 schema 未声明对象身份或业务源无对应快照时，保持 unknown/缺口，不能从 oracle 反推配置或补造数据。出站与按需资料收据由实际事件和每次 HTTP 时已发生的读取记录导出，后来的补读不能追认此前发送的正文。

执行观察 `Observation` 包含 run/detail、已保存证据、调用记录、模型实际收到的请求、源实际收到的请求、API 操作收据与运行计量。原始观察默认仅写指定仓库外目录；公开摘要只包含合成断言、计数与边界。基线与候选共用冻结套件；首轮结果不覆盖，重试带 `retry_of`，不得择优隐藏首败。

真实 MiniMax 配置必须由调用者显式提供为当前用户拥有、仓库外、非符号链接的普通 `0600` JSON；HTTPS 地址不得内嵌凭据或查询串。密钥仅存在于内存及子进程临时加密库，不保存配置、认证头或 provider 原始错误。没有自动 fallback。真实模型与合成模型分别标记，不混为可比基线；本轮真实源适配与真实案例重验收留待后续授权材料接入。

## 独立评分

oracle 是显式断言集合。每个断言有 id、dimension、operator、expected、critical 和工作包范围。支持确定性字段、文本正则、集合包含/禁止、数值边界和引用存在/来源正确性。断言执行错误算失败，不能跳过。每案例输出 `strict_pass` 与事实/遗漏/因果/覆盖/引用/边界维度的 passed/failed/unknown/total；全部适用断言通过才是严格通过。重大错误不可被平均分或低成本抵消。未运行案例保持 `not_run`，能力尚无可观察接口返回 `unverified` 并使严格结果不通过。

失败分类至少为 factual_error、omission、unsupported_causality、coverage_overclaim、invalid_reference、source_failure、model_failure、privacy_violation、authorization_violation、recovery_gap、unverified_capability 与 harness_error。同一失败可计入分项，案例总数与断言总数分别对账。

## 计量

记录 wall/queue/execution elapsed、model/tool/source/control request、retry/cache、输入/输出 token。优先采用 Runtime 实际计量和拦截的真实请求次数，另存两者差异。没有提供的值为 `null` + reason，不补零。价格缺失时费用 `null`；真实定价须含币种、单价、版本/日期和适用范围。合成模型不虚构 token，且无真实模型成本测量。

## 基线可复现与限制

基线源码固定完整 SHA，输入/预期/评分器各有摘要；执行和评分分进程，标准化随机 run/evidence 标识只用于比较，不改原始观察。案例分项可重复，耗时和实际随机 ID 不要求字节一致。SQLite 只验证合成运行路径，不冒充 PostgreSQL 恢复或业务验收；一致恢复案例在没有对应操作收据时保持未验证。当前版本可对未来能力失败；R01 完成条件是基线完整和可信，不是所有案例通过。

当前冻结结果为 legacy 严格 8/15、78/88 断言通过；M1 同套件旧版严格 7/18，候选 15/18，候选断言 102 passed、0 failed、3 unknown。两套分数不能跨适配器解释为提升。原始早期失败、schema 修正前矩阵与每次重试保留在仓库外；仅登记已知合成 reading 字段的修正属于 fixture 修复，不计为应用改善。共享撤权、人工裁决、一致恢复三项未知保持严格不通过。三次真实 MiniMax + 合成源业务尝试为 failed/partial，成功的独立模型协议往返不能替代业务验收。
