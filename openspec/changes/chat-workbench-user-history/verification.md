# Verification

变更：`chat-workbench-user-history`。本文件保留历史规格映射，已去除现场数据、运行标识、原始收据哈希和私有材料索引。它是公开投影，不是原始在线证据，也不是当前公开提交或新环境的验收。

所有历史开发、运行与检查在 Mac 本机执行；正式 HTTP 使用 PostgreSQL。公开图片完全新制，不作为这些历史检查的截图。

## Historical Status

| 检查层 | 历史结果 | 证明范围 |
| --- | --- | --- |
| 后端回归 | 84 passed | 配置、身份、会话、附件、归属、AG-UI、多轮与恢复边界；合成模型不是在线业务验收 |
| Node 代理 | 通过 | 四类非法目标/Host 拒绝，正常请求只转固定上游 |
| 前端构建 | 通过 | TypeScript/Vite，仍有上游大分包提醒 |
| PostgreSQL HTTP | 16/16 | `live_model=false`，用户隔离、附件和登录恢复 |
| Chat 及服务端联核 | 19/19 | 两轮真实模型报告、持久化、刷新幂等与下载一致性 |
| 桌面/手机/键盘 | 通过 | 1680×820 与 390×844；输入框、无整体横向溢出、焦点恢复及控制台检查 |
| 当时 OpenSpec | 5/5 | 三项主规格同步，15 个 requirement；不指当前变更集合 |
| 当时凭据和 diff | 通过 | 只针对当时已知凭据与检查范围，不等于全面环境去标识化 |
| 用户接受 | 待反馈 | 功能链路和模型报告不等于完整业务影响/根因验收 |

## Spec Mapping

下表覆盖本次三项 delta spec 的 15 个 requirement；“通过”仅针对列明证据，浏览器最终结果与业务接受分别记录。

| Spec / Requirement | 实现与验证依据 | 历史结论与边界 |
| --- | --- | --- |
| `chat-workspace`：产品入口与文案一致 | `web/src/pages/Workbench.tsx`、`App.tsx`、`components/Identity.tsx`；最新构建；真实 UI 已进入 Chat、保存上下文并发送 | 主入口、账号区和分析文案已实现；受影响告警/Skill/MCP/账号入口回归及文案检查通过 |
| `chat-workspace`：Chat 与分析画布并列 | Workbench、Composer、AnalysisCanvas；实际 Chat 发送与刷新，报告/文件留存收据 | 桌面三栏与手机 Chat 优先通过；同 ID 证据联动、显式画布与固定输入框已实际核对，见 `web/QA.md` |
| `chat-workspace`：上下文可见且缺失可补充 | `test_private_history_idempotency_missing_context_and_reload`、`test_context_type_errors_and_no_unscoped_tool_permission`；HTTP 的 `missing_context_saved_not_executed`；UI 动作记录含缺上下文提示及保存 Skill/目标 | 问题持久保存，缺输入明确不执行；不复制 Demo 中的目标、指标常量或结果 |
| `chat-workspace`：持续对话与独立分析轮次 | `test_followup_retains_old_round_and_builds_bounded_trusted_history`；AG-UI 活跃上下文参数化回归；UI 收据 `two_rounds_saved`、`followup_parent_preserved`、`first_report_unchanged` | 两轮独立留存与关联通过；历史摘要有界，旧证据不自动进入本轮有效证据集；两轮均 partial |
| `chat-workspace`：发送和恢复幂等 | `test_explicit_terminal_conversation_accepts_new_message_and_replays_once`、`test_explicit_terminal_legacy_message_retries_once_by_message_id`、`test_pending_supplement_recovers_original_terminal_run_receipt`；UI `browser_reload_no_duplicate`、`terminal_duplicate_no_dispatch` | 同消息、同/新协议重连和持久补充回执恢复通过；空消息仅订阅；模拟中断不是完整断电场景 |
| `chat-workspace`：可读状态与键盘操作 | `test_finish_maps_consumed_and_late_messages_and_keeps_one_report`、`test_queued_cancel_finishes_conversation_once_without_model_claim`；UI `user_consumption_truth`，两轮 partial 均保留 | 消费/未消费、取消模板、部分覆盖状态通过；Enter 打开证据、Escape 关闭恢复原按钮、手机画布焦点及控制台均通过 |
| `user-history`：服务端账号与角色 | `test_identity_roles_compatibility_and_disable`；HTTP `operator_configuration_rejected`、`operator_test_purpose_rejected`；generic submit / AG-UI 试调绕过回归 | 独立用户名、服务端 session、管理员管理用户与配置、operator 正式分析边界通过；不提供开放注册或 SSO |
| `user-history`：私有数据归属不可伪造 | `test_foreign_links_and_mutations_do_not_reveal_or_modify_a_run`、HTTP run/SSE/evidence/mutation 与 AG-UI 双 owner 用例；HTTP `conversation_cross_user_404`、`admin_private_history_404`、`attachment_cross_user_404` | 私有入口按父对象归属；管理员也不能读取其他用户历史；长订阅检查停用/撤销 session |
| `user-history`：共享视图不泄露私有执行 | `test_overview_filters_owner_and_never_contains_private_snapshot_paths`、`test_request_keys_and_internal_default_are_user_scoped`；HTTP `conversation_user_scoped_idempotency`、`conversation_list_isolation` | overview/queue/recent/invocations 过滤归属；相同客户端 key 不跨用户合并；配置目录共享不返回凭据 |
| `user-history`：服务端持久会话历史 | `test_conversation_history_survives_default_run_expiry_and_finish_hook`；HTTP 重新登录附件恢复；UI `owner_retention`、`relogin_same_messages_rounds_files` | PostgreSQL 会话、消息、轮次和文件跨刷新/重新登录恢复；旧七天清理不删除会话 run；不声称高可用或完整断电恢复 |
| `user-history`：初版数据兼容 | `test_old_session_admin_history_and_actual_request_limit`、HTTP legacy admin 归属用例；保留旧口令登录方式 | 无 owner 旧记录仅归 admin，未清库、未重写旧证据；旧 51 项结果另列历史基线 |
| `conversation-attachments`：附件持久保存与授权下载 | `test_attachments_owner_paths_extraction_and_cross_conversation`、`test_user_quota_and_symlink_cannot_read_other_file`；HTTP `attachment_exact_bytes`、`attachment_download_disposition`、`attachment_cross_user_404`、`attachment_metadata_no_path` | 原始字节与安全下载通过；文件键由服务端生成，用户文件名不作磁盘路径；跨会话引用拒绝 |
| `conversation-attachments`：上传边界与失败明确 | `test_attachment_limits_request_stream_and_failure_cleanup`、用户配额/实际请求大小用例；HTTP `invalid_upload_rejected`、`failed_upload_no_metadata` | 单件 5 MiB、用户 100 MiB、请求 8 MiB 与无效编码的边界由合成用例覆盖；实际 HTTP 验证正常小文件与失败状态，不声称大文件压力测试 |
| `conversation-attachments`：附件作为有界用户材料 | 材料提取/跨会话用例、`test_live_materials_are_validated_frozen_and_not_duplicated`；UI 原生文件上传及 `public_projection_no_internal_materials` | UTF-8 文本每件 12000 / 合计 24000 字符，有截断说明；材料不作源证据或可执行指令；二进制可保存但未提取，未声称模型读过二进制 |
| `conversation-attachments`：生成报告制品可恢复 | `test_finish_reconciliation_reports_idempotent_and_unconsumed_message_visible`、`test_detail_retries_report_artifact_and_refreshes_same_assistant`；UI `two_report_files`、各 `download_sha_*` / `download_report_body_*` | 两轮独立 Markdown 的内容、下载哈希与原轮对应；重复结束不重复助手/文件；瞬时失败恢复清旧错误，原报告不覆盖 |

测试文件：`backend/tests/test_chat_services.py`、`test_runtime_ownership.py`、`test_agui.py`、`test_runtime.py`、`test_config.py`。完整测试为 Mac 本机执行，单测使用独立 SQLite 合成环境；本轮正式 API 证据来自 Mac PostgreSQL，两者不混称。

## Real Model Evidence

历史连续追问的两轮都产生真实外部模型报告：`source=model`、`model_generated=true`，终态均为 `partial`。第二轮关联第一轮，首轮报告和文件保持不变；两份助手收据、消费状态、授权下载内容与重新登录恢复完成联核。

公开摘要不保留会话、轮次、证据和报告标识，也不保留报告或收据哈希。历史供应商配置已移除，不能把这些结果归为后续优先模型的验收。两轮链路通过不确认全部依赖、业务影响或根因。

## Preserved Failure

首次真实模型尝试中，一轮模型请求超时，另一轮在探索结束后报告回合未完成，最终均使用 `platform_template`、`model_generated=false`。最初 oracle 把 analysis 非空和模型被调用当作真实报告，已经纠正为 `passed=false` 并保留首败。后续成功没有改写旧报告或把失败回填为成功。

## Final Closeout

- 历史桌面和手机核对完成，Chat 摘要约 360 字，完整报告保持原文；窄屏刷新不自动打开画布，显式打开与 Escape 关闭恢复焦点。
- 证据引用实际打开对应条目并恢复原按钮焦点；只读页面核对没有提交新分析。
- 告警到 Chat 的上下文预填、Skill 草稿、MCP 目录/schema/试调表单和账号入口回归通过；未提交表单不计作工具或模型执行。
- 三项 delta 同步至主 specs，现有只读执行基线保留；临时上传源文件清理，服务器会话与附件保留。
- 用户接受仍待反馈。首次公开与当前去标识化分别由独立变更记录，不能将旧验收结果移用于当前配置改动。

首版 51 项测试与更早分析样本见[交付状态](../../../docs/status.md)。它们不替代本轮新增能力，也不证明当前提交或远端状态。
