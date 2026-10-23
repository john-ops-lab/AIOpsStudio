# R04 结构化报告验证

日期：2026-10-23。使用 Mac 隔离合成 JSON 与保存材料，不读取真实业务源、私有配置或既有运行实例。真实 MiniMax、浏览器流程、最终冻结全量和业务/用户接受由[本轮总验证](../../../docs/validation/2026-10-23-m1.md)分别记录。

## 独立事实核对

初版 `test_grounded_reporting.py` 30 passed，覆盖每条 Prometheus matrix/vector 序列的 first/last/min/max/count、尾部与第二序列、稳定事实/版本结论、值/单位/计数/方向/窗口、保存/传输完整性、正常/异常冻结规则、同源去重、冲突和自由文本未知。失败源不能用内部片段确认事实；完整原子页面最多支持本页，不把 source partial 改为全量。

独立审查先证实“查询目标被赋给每个返回对象”和“数字正确就把整句模型解释视为核验通过”。修复后模块为 39 个场景，联合当时 7 个审查场景共 46 passed：

- result_identity 版本、逐行/series 身份、来源 mapping 及四维范围全部匹配才给 fact.object；缺规则/字段、异源/异对象/异范围保留 query_scope 和原值，object=null/saved_material_only。第二序列确为其他对象的 oracle 保留，不改成同一目标来取绿。
- 模型原句保存为 model_text，wording_status 未核验；text 由程序生成结构化坐标/规则/值。结构化数值一致不能确认任意模型因果，根因始终 undetermined。
- 缺真正时间绑定的拓扑不能冒充故障时刻关系，由 R02 同步限制；图上可达最多潜在关系，不是已发生影响。

随后独立审查两项错 anchor 首败：只读 public 字段却允许 secret 引用；时间筛选只返回原始 row1，却用视图 offset=0 支持 row0。R03 添加原始 source_indices/source_total_items/projection_fields，R04 按其校验。对应两项保持原 oracle 后转绿，联合为 48 passed。旧筛选 anchor 缺定位元数据保守拒绝；全数组聚合须覆盖全部原始下标且无筛选/投影。

## 集成与状态

某一阶段完整后端 392 passed；后续新增测试后的最终冻结结果以总验证为准，不重复使用旧计数宣称最新全量。运行集成先核对 owner/源范围和保存摘要，再调用纯报告模块；中文界面已有分层和精确片段入口，但代码存在不等同浏览器流程或用户接受。

`openspec validate ground-alert-impact-reports --strict` 与 `openspec validate grounded-reporting --type spec --strict` 通过。主规格已同步返回身份、结构化/原句分层和片段原始坐标合同。

无已登记单位/阈值、未知 schema、来源陈旧/缺页、跨对象或未知历史保持 partial/unknown；不推断零、恢复正常或完整名单。R12 人工裁决/根因确认和 R13 Case 共享仍未实现；真实业务与用户接受保持待验，不能用合成结构核对或模型接口成功代替。
