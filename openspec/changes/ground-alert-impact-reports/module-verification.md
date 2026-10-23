# R04 纯模块验证

2026-10-23，Mac 隔离合成 JSON，无网络/模型/私有配置/现有实例调用。

- `backend/.venv/bin/python -m pytest backend/tests/test_grounded_reporting.py -q`：30 passed。
- 覆盖所有 matrix/vector 序列及末端；稳定 fact/版本 claim；数字、单位、计数、关系方向、对象窗口、片段锚点；保存/传输不完整与校验拒绝；partial/陈旧/历史未知；冻结正常/异常评估；根因/候选不确认；潜在拓扑不冒充实际故障；同源去重、冲突/正常对照/末端反证；自由文本与未知 schema 不自证。
- 首次 30 项通过。审查后加强失败源拒绝内部事实、分段整体核验、计数 items 单位与非数值无业务单位，原 30 项仍通过。

仅证明纯模块语义；运行时接入、真实出站、界面定位、R01 候选比较与业务接受由集成负责人分别记录，不以此记录代替。

## 独立复核后的返回身份修复

独立复核先发现：无真实时间绑定的 topology、不同返回对象被查询 canonical_key 冒充。未修改复核断言。R02 负责拓扑时间门槛，R04 增加显式 result_identity 合同，返回对象按每行/series 身份与四维源分区核对，缺失/不匹配保留 saved_material_only。root 同时将模型原句与程序生成结构化 text 分开，不能靠匹配数字认证原句因果。

`backend/.venv/bin/python -m pytest backend/tests/test_grounded_reporting.py backend/tests/test_review_regressions.py -q`：46 passed（39 个模块场景与 7 个独立复核场景）。新增覆盖：缺契约/版本、错对象、缺 scope 维度、分区冲突、异源、逐 series 范围字段、singleton 不冒充任意行、canonical.id 选择及正常/异常不能升级外部对象。测试 oracle 保留第二序列不同对象：值可确定，但对象未知，不再把整体报告宣称 verified。

## 片段复核与最终记录

投影字段与时间筛选错锚点两项独立首败修复后，当时模块+审查联合 48 passed；完整后端曾达 392 passed。后续新增测试和最终冻结状态见 `verification.md` 及总验证，不以此旧计数代替最新 HEAD。
