# R07 授权与模型出站验证

日期：2026-10-23。此记录覆盖 Mac 独立开发副本中的实现和合成回归；不包含真实业务源或用户接受。真实 MiniMax 和最终代码冻结结果由[本轮总验证](../../../docs/validation/2026-10-23-m1.md)统一记录。配置、凭据原文、源端点、用户数据和原始请求不进入此记录。

## 已验证边界

- `test_access_policy.py`：33 passed。双用户/admin 无隐式权，固定/最新策略交集、逐次撤权、工具/目标/环境/目的、参数全叶约束、凭据/连接/TLS、类型/字段白名单、未知类型失败关闭及 owner/会话隔离别名。
- `test_trust_api.py`：13 passed。真实 FastAPI/认证/Store/MaterialReader/附件服务配合无网络授权适配器，验证策略 CAS/不可改写版本、已知凭据拒绝保存、片段原 selector/摘要/预算/最新权限、递归历史与报告读取/下载限制及普通用户目录/概览最小可见。
- 连接器/运行时联合合成测试在真实 MCP/模型 SDK 序列化后的 HTTP transport 捕获请求，覆盖初始化、目录、调用、流式会话结束、登录重试/分页、同源重定向、精确目标、绑定认证、模型协议与附加参数。请求被拒绝时验证未发生下一次派发，不把测试 monkeypatch 成无授权连接器。
- 独立复核保留“先准备上下文后收紧出站策略”“未知 extra_body”“已撤销本地凭据仍属禁止值”“普通模型历史读取被修改材料”首败，分别加入回归。固定错误文案替代源/模型原始异常正文；未以此声称任意未来日志/导出格式均已穷举。
- 来源撤销后，列表/详情/概览/继承历史/生成报告附件均受限；管理员不能读取其他 owner 私有内容。旧无来源的派生正文保持拒绝，不为了兼容合成测试升级成可信。

## 首败与修复记录

初次完整后端回归为 19 failed / 313 passed，主要是既有合成 fixture 缺新策略、明确对象/窗口或来源元数据。只给原有意图明确的 fixture 登记静态版本化合同，不在生产加入 bypass、不动态允许当前 HTTP 所有字段；原断言保留，未知旧材料另测拒绝。

独立审查随后发现返回对象/拓扑时间和片段字段/原始坐标缺口，按审查断言修复而非改 oracle。某一代码阶段完整后端达到 392 passed。之后新增 operator 安全回归及其他模块测试，完整回归出现 2 failed / 398 passed：旧概览 fixture 无源授权、合成模型 override 签名与 SDK 不符。前者补明确合成 owner/源策略后，chat+trust API 定向 27 passed；后者由主集成修复并单例通过。最终冻结全量尚由主集成统一记录，此处不把较早全量结果当作最终 HEAD 通过。

## 校验与限制

`openspec validate enforce-source-access-scope --strict` 与 `openspec validate source-access-scope --type spec --strict` 通过。本变更主规格与 delta 同步；固定案例和 R01 独立答案未因候选实现而改写。

未重新连接真实业务源，未切换现有运行实例。合成 SDK HTTP 捕获不是远程 provider 业务验收；真实 MiniMax 按总验证单独记录。R13 Case 共享/撤回、未来新增导出、全 provider/schema 组合仍需各自授权矩阵和验收。业务接受、用户接受与 PR/合并/部署不能由代码或合成检查代填。

## PR 15 visible-revocation regression

The first review found that resource refresh failures only updated the error message, preserving previously rendered report/conversation data. The frontend now clears protected data on authoritative 401/403/404 and policy-unavailable 503 responses; an open evidence modal periodically rechecks evidence authorization and discards local fragment state on denial. Transient server/network errors retain content with the error visible.

Mac Chrome extension checks used the actual built frontend with an isolated synthetic HTTP fixture, without production configuration, database, model or source access:

- An already-open conversation, report and exact fragment disappeared after polling returned 404, without a page reload.
- A 500 transient-error control kept the existing fragment visible with its error.
- With the conversation still readable, denying only evidence metadata cleared a locally POST-read fragment from the still-open modal. The exact-fragment GET count stayed at one; periodic authorization checks did not consume fragment-reading budget.
- Page identity, meaningful rendering, native controls and screenshot were inspected. The first fixture omitted the Copilot runtime-info schema and produced three fixture-related console entries; the fixture schema was corrected, with no additional console warnings/errors in the final interaction. This is a desktop scoped regression, not mobile or real business end-to-end acceptance.
- Final frontend production build passed (1.95 seconds), with the existing large-chunk warning. No runtime/model configuration or user data was changed.

## PR review: source response revoked in flight

独立回归在源协程返回前保存新的deny，旧代码未抛出PolicyError且先写入响应。修复后在同一Store原子操作内、任何证据文件写入前重新核对source范围；PolicyError终止，不生成源正文或替代失败证据，仅保留固定失败调用状态。真实source invocation scopes同时纳入历史授权，避免没有evidence时从调用参数/事件读取撤权源标识；model协议invocation不伪装成源授权。过期调用清理保留owner/scope供后续授权，对外仍移除scope。定向race及过期回归通过，完整结果由主集成登记。

旧数据兼容以 `preserve-legacy-history-on-upgrade` 的显式只读清单为限，不改变新运行的源授权与模型披露规则。
