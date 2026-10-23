# 分析案例与独立评估（R01）

固定源码基线为 `bd9a949cff85e2f5b00b5dfd43d323909bfd0a1f`。保留两个独立套件：最初 `suite.json` 为 legacy 15 案例/88 断言；`m1/suite.json` 为 M1 18 案例/105 断言。两者使用不同的冻结配置和模型适配器，不能直接比较分数。各自的 `inputs/` 与 `oracles/` 严格分开，配置、源码、评分器及内容摘要固定。

## 运行方式

在 Mac 本机、仓库根目录使用已安装依赖的 Python；默认不联网，不读取真实模型配置，不启动常驻服务。输出目录必须是仓库外的全新目录。

```sh
backend/.venv/bin/python scripts/evaluate_analysis.py \
  --baseline-only --attempts 2 \
  --output /tmp/aiopsstudio-evaluation-baseline-001
```

保留旧版测量后，运行当前工作树并比较（也可 `--candidate-ref <commit>`）：

```sh
backend/.venv/bin/python scripts/evaluate_analysis.py \
  --compare-with /tmp/aiopsstudio-evaluation-baseline-001/baseline/summary.json \
  --candidate-ref working-tree \
  --output /tmp/aiopsstudio-evaluation-candidate-001
```

也可省略 `--compare-with`，工具会先测固定旧版，再测候选版。`--case normal-alert` 可调试单例，未执行的其他案例仍留在分母，标为未执行。`--attempts 2` 指两个隔离评估尝试；不等于生产运行的 Retry API，也不代表 provider 内部重试可观测。统计始终使用第一个尝试，后续尝试只检验重放一致性，不择优替换首败。已存在目录或文件拒绝覆盖。

M1 必须显式选择相同套件进行前后比较：

```sh
backend/.venv/bin/python scripts/evaluate_analysis.py \
  --suite evaluation/m1/suite.json --attempts 2 \
  --output /tmp/aiopsstudio-evaluation-m1-001
```

`baseline/`、`candidate/` 包含每个尝试的可见执行输入、实际观察和摘要；`comparison.json` 比较同一套件。实际源快照或冻结版本不明、输入/可见性/模型/评分器变化时拒绝可比结论。不同模型测量各自标记，合成模型与真实模型不能直接混为同一基线。

## 实际执行路径

每个尝试启动独立子进程，使用选定源码的 `aiopsstudio.main.create_app(testing=True)`、空 SQLite 和临时制品目录，经登录、`POST /api/runs`、真实单 worker/Deep Agents、GET run 与证据 API 获取结果。候选工作树先复制并计算代码摘要，所有案例运行同一份源码，防止并行修改导致版本混用。不会绕过运行管线直接把预设报告交给评分器。

合成源返回固定时点可见快照。所有案例共用一个保守合成模型：只看运行时真正交付的消息，调用注册只读工具，从实际 evidence ledger 逐字段报告；不会接收 case ID、源快照全文、oracle 路径或预设答案。执行子进程禁止读取 `oracles` 路径；父进程等执行结束后才加载对应 oracle。评分器只做确定性断言，不调用模型。

这衡量应用取证、阅读和边界的合成保真性；不是 MiniMax 准确率或业务根因验收。SQLite 回放也不是 PostgreSQL 一致恢复验收。

M1 使用 `synthetic-wire-extractor-v2`：保留真实 ChatOpenAI SDK、应用 HTTP request guard 与 Deep Agents，仅将 HTTP transport 替换为离线 MockTransport。合成响应按真正收到的 HTTP 消息/工具 schema 生成，记录实际 body，不接收 oracle。身份和时间由服务器绑定后，适配器只传实际暴露的可变参数；尾部补读依赖真实返回的保存数量，参考资料读取依赖实际 manifest。`m1/frozen.json` 的类型/字段策略只登记固定合成 schema；未知字段仍拒绝，测试证明不会因构建 fixture 自动允许新字段。

## 结果口径

案例严格通过要求所有适用断言通过，未知、未执行和评分器错误均不通过。事实、遗漏、因果、覆盖、引用、边界各自记录 passed/failed/unknown/total，案例和断言的分母分开。禁止关键错误被平均分或费用抵消。

记录实际模型、工具和发现请求以及 Runtime 计量，MCP 内部 HTTP、provider 重试/缓存、缺失 token 和价格明确未知。没有提供用量收据时 token 为 `null`，包括模型调用失败而 Runtime 仍保留初始零计数的情况；费用没有冻结定价表即为 `null`。排队、执行和总耗时按实际运行保存，不能据此宣称真实模型性能。

未来出站、共享/撤权、人工裁决、references、恢复样本保留实际收据断言。当前未实现步骤写入 `unverified_operations`，不会自动伪造操作；缺少收据计 unknown。后续工作包用相同观察合同接入实际操作收据，再由独立评分器核查。

## 当前公开基线

[bd9a949 合成基线摘要](baselines/bd9a949-synthetic-v1.json)：15 案例严格通过 8 项，7 项不通过；88 断言中 78 通过、4 失败、6 未验证。15 案例分别重复两次，评分全部一致；保留第一个尝试。失败覆盖尾部明细漏读、无关证据混入、敏感字段模型出站和未按需注入 references；共享/裁决/恢复等没有操作收据，保持未验证。

冻结前发现的两个评估器问题及修正记录在摘要中；早期观察保留在仓库外，修正不计为应用效果改善。原始模型请求、run/证据 ID、临时路径和完整收据不进入公开摘要。

[M1 同套件前后比较](baselines/m1-trusted-synthetic-v1.json)：固定旧版严格 7/18，83 passed、16 failed、6 unknown；候选严格 15/18，102 passed、0 failed、3 unknown。较早冻结的旧版与候选各 18 案例重复两次，评分全部一致；应用收口操作员边界后，复用同一旧版基线完整重验最终候选一次，18 项评分与此前一致，重大退化为零。摘要分别记录重复范围与最终代码摘要。剩余三项是共享撤权、人工裁决和一致恢复，仍在严格分母中；不能写成 18/18 或全部未来能力已完成。

M1 首轮保留 14/18、101 passed/1 failed/3 unknown：已声明的两个 `reading` 窗口 `reason` 元数据字段未登记，守卫正确拒绝。手审补齐固定 schema 后再次冻结并前后重放，未改变源快照或 oracle，也未修改生产守卫提高分数。M1 与 legacy 的原始观察和首败均留在仓库外。

## 显式真实模型测量

只有显式指定 `--live-model-config` 才允许实际模型网络调用；源仍为固定合成快照，模型优先 MiniMax，不静默切换。配置必须是当前用户拥有、仓库外的普通 `0600` JSON，含 `provider: "minimax"`、`model`、`base_url`、`api_key`，可选 `max_tokens`。另一 provider 需要显式 `explicit_provider_override: true`。密钥只在内存/子进程临时加密库使用，配置和密钥不写入观察或公开摘要。错误只保留固定分类，不回显 provider 原文。

```sh
backend/.venv/bin/python scripts/evaluate_analysis.py \
  --baseline-only --case normal-alert \
  --live-model-config "$MODEL_CONFIG_FILE" \
  --output /tmp/aiopsstudio-evaluation-live-001
```

公开合成基线没有调用真实模型。[真实 MiniMax + 合成源单例记录](baselines/minimax-synthetic-source-check.json)另记三次有限尝试：首次接口路径失败；后两次取得实际模型响应/工具调用，但分别受合成参考资料 schema 和固定源/选择器缺口限制，均为 partial。实际 HTTP 均经过守卫；没有换用其他模型、补造源数据或把失败改称业务通过。真实模型配置和原始 HTTP body 仅留仓库外。独立模型协议往返由集成验证另记，不能替代这三个业务尝试。

本轮未连接真实业务源或重新验收真实案例。经授权真实材料可后续按相同可见输入、独立 oracle、来源与版本合同适配；`score(observation, oracle)` 可复用，不代表本轮已完成真实业务验收。
