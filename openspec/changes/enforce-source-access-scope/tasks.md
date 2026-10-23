## 1. 策略与纯模块
- [x] 1.1 实现版本化源策略、目标/参数/凭据/连接校验及 deny 优先。
- [x] 1.2 实现快照与最新策略交集、结果及历史材料来源授权。
- [x] 1.3 实现模型类型/字段白名单、凭据禁止与 owner/conversation 隔离别名。
- [x] 1.4 完成双用户、撤权、失败关闭、连接绑定、别名与事实关联合成测试。

## 2. 集成与验收
- [x] 2.1 接入运行提交/领取、源派发/返回、试调、补读、递归历史、会话、概览及生成报告读取/下载边界。
- [x] 2.2 在真实 SDK HTTP transport 接入逐请求检查，并以合成捕获验证策略 race、协议/附加参数、重试、同源重定向和零派发拒绝。
- [x] 2.3 完成策略管理 CAS/不可改写版本及普通用户配置选择最小元数据，保留 admin 无隐式数据权。
- [x] 2.4 记录首败与修复、适用边界和未完成 Case 链路，补充两个主规格并完成严格校验。
- [x] 2.5 完成纯策略、真实 API/保存材料、历史/报告与 operator 选择入口定向回归；记录当时完整 backend 通过及后续新增后的修复状态。
- [x] 2.6 主集成完成最终代码冻结后的统一回归、公开安全检查、完整设计/验证汇总和可审查 PR/Issue。
- [x] 2.7 按独立门槛记录真实 MiniMax、真实业务源、业务接受和用户接受；不以合成 HTTP 或接口成功替代。

## 3. 后续范围
- [ ] 3.1 R13 实现 Case 共享/撤回后，对每位读者及新增导出形式单独验收；当前仅提供约束合同。

集成记录：[PR #15](https://github.com/john-ops-lab/AIOpsStudio/pull/15)、[M1验证](../../../docs/validation/2026-10-23-m1.md)。以上“记录”任务表示已分别登记真实模型、业务和用户状态；真实业务源未验收，用户接受待反馈，后续范围仍未完成。

## 4. PR review corrections

- [x] 4.1 Clear denied cached report/conversation data and already-open saved fragments after policy revocation.
- [x] 4.2 Verify current-page revocation and a transient-error control without replacing production state.

- [x] 4.3 源响应在途撤权在写盘前重验；调用历史继续重验scope并覆盖过期元数据回归。
