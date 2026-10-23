# R07 纯模块验证

日期：2026-10-23。范围：Mac 隔离工作区中的策略纯模块、合成 Store SQLite、OpenSpec。未访问真实源、模型、私有配置、生产数据库或原运行实例。

- `backend/.venv/bin/python -m pytest backend/tests/test_access_policy.py -q`：33 passed。
- `openspec validate enforce-source-access-scope --strict`：通过。
- 首次 30 项通过；增加管理员完整草稿校验、最新字段收紧和别名回填→目标再次授权后 33 项通过，无失败重跑。

覆盖双用户/admin 无隐式权、目标/环境/工具/目的、参数全叶白名单、明确整源权、TLS/目标/Host/重定向/凭据精确绑定、排队/运行/历史撤权、冻结不能扩大、缺失/读取失败/未知类型、凭据字段及正文、数组通配不能扩大对象字段、别名跨轮稳定/会话用户隔离、未知/嵌入 query alias 拒绝、允许事实数字保持、映射不进入模型返回或快照。

本记录不宣称运行时、HTTP、日志/事件/导出全链路或 Case 已通过。任务第 2 组由集成负责人完成，必须捕获实际模型请求字节并验证源返回及历史读取拒绝。

## 管理和历史 API 合成验证

- `backend/.venv/bin/python -m pytest backend/tests/test_trust_api.py -q`：11 passed；真实 FastAPI/身份/Store/MaterialReader/附件服务，runtime 接口使用无网络合成授权适配器。
- 策略 CRUD/CAS/不可改写版本与角色隔离；已知凭据拒绝持久化；材料读取/片段实际保存字节与摘要/当前源范围/预算；默认策略缺失及固定异常文案；MCP catalog 显式 grant、派发 observer 与返回时撤权；旧设置不升级/新格式错误不保存。
- 会话列表/详情与生成报告 metadata/阅读/下载均在源撤权后拒绝；管理员不可读 operator 私有报告，缺授权器不放行，空会话可初始化，原用户 upload 保持 owner 访问，保存报告被修改后下载拒绝。
- 原 `test_config.py` 与 R07/R04/API 联合定向检查（新增历史测试前）92 passed；历史新增后 11 项 API 再通过。尚未把纯适配器结果当作完整运行时或真实源/模型业务接受。

## 收敛记录

后续集成与独立复核已形成 `verification.md`，包含最新 13 项 API、operator 最小可见和派生历史回归；此前纯模块/11 项记录保留为当时结果，不作为最终全链路结论。最终冻结回归与真实 MiniMax 由主集成统一记录。
