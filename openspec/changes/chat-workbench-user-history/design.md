# Design

> 历史功能设计的公开副本。现场标识已移除；仓库现有图片完全新制，仅作通用演示，不代表当时验收截图。当前行为以主规格为准。

## Context

见 proposal 的 Why。当前 React Runs 页面围绕 run 展示报告，Chat 为附属面板；认证仅恢复 admin。运行状态为 PostgreSQL JSON 文档和持久事件，原始证据是服务端文件。保留现有单执行槽、Deep Agents、只读源与 CopilotKit/AG-UI 投影。实现时依据当时确认的界面要求；当前公开 [Chat 通用演示](../../../docs/design/assets/20261022-AIOpsStudio-Chat工作台Demo-v0.1.png)仅展示布局方向，不能作为原验收截图。

## Goals / Non-Goals

**Goals:** 会话独立于一次分析；所有传输入口使用服务端 owner；真实历史/附件跨登录恢复；按效果图完成可操作 Chat、画布与用户入口。

**Non-Goals:** 不实现组织租户、SSO、开放注册、任意二进制解析、自动修复、自动删除会话或新的部署平台；不改已有 API purpose=`investigation` 等技术标识。

## Decisions

### 1. 独立用户与可信身份

引入 identity 服务，用户文档保存随机盐和 PBKDF2/scrypt 口令派生值，Cookie 仅保存随机 token；session 通过哈希索引恢复有效用户和角色。初始 admin 从既有部署口令引导，旧 session 兼容归 admin。管理员创建/停用普通用户；普通用户读取可用配置目录，但仅 admin 修改共享配置、管理用户及审计。管理员也必须按自己 owner 读取私有历史。拒绝前端用户名切换或 payload owner 作为身份方案。

### 2. 会话与轮次

conversation 文档包含 id、owner_id、title、context、messages、时间与客户端幂等信息。run 的 conversation_id 与 thread_id 指向 conversation，新增可信 owner_id、parent_run_id 和历史上下文；旧未归属记录只对 admin 可见。Runtime.submit/message/cancel/retry/detail 增可信 owner 参数，内部兼容调用默认 admin；HTTP 必须显式传当前身份。幂等键以用户为范围，消息另含会话。

会话 API：GET/POST `/conversations`，GET/PUT `/conversations/{id}`；POST `/conversations/{id}/messages` 接 `{content, client_message_id, attachment_ids?, run_input?}`，返回 `{message,run,conversation}`。会话详情包含持久 messages、各轮 runs 与 attachments。`run_input` 与保存的 context 合并，owner/关联字段由服务端覆盖。缺少必需上下文时先保存问题，返回明确未执行/待补充提示；补齐后用新的消息标识显式再次提交。

同上下文运行中补充用现有 runtime.message；终态补查或主上下文变化创建新轮次。新轮传入前轮用户问题与报告摘要的有界快照，明确标为历史，不把旧 evidence_id 加入本轮有效证据集。结束回调将最终助手消息和报告卡片按 run_id 幂等存入会话。会话关联 run 默认 retained，现有 7 天清理不能删除其历史。保留全局容量拒绝机制。

### 3. 附件存储与模型材料

附件 bytes 写入服务端 artifacts/attachments 下随机 ID 文件；元数据在 PostgreSQL，保存 owner、conversation、name、size、sha256、media_type、kind、run_id 与提取状态。JSON base64 上传避免引入 multipart 解析依赖；仅上传入口放宽至 8 MiB 请求体并实际流式计数，解码上限 5 MiB，单用户总附件 100 MiB。写文件和保存元数据失败时清理本次文件。GET 附件下载强制 Content-Disposition attachment、nosniff、校验父会话归属；不暴露 path。

接口：POST `/conversations/{id}/attachments` 接 `{name,media_type,content_base64}`；GET `/attachments/{id}` 返回 metadata；GET `/attachments/{id}/download` 下载；会话详情提供附件列表。服务接口 `reading_context(owner_id,conversation_id,attachment_ids)` 返回 UTF-8 文本、摘要、截断/未提取标志，无文件路径；单件至多 12000 字符，总量至多 24000 字符。Runtime 从 ID 取可信材料，不能接受前端直接写入模型材料。终态 `finish_run(run)` 调 `save_report(run)` 保存 Markdown 制品，按 run_id 幂等。

### 4. Chat 视觉与交互

沿用 React/Vite 与 lucide 图标。token：navy #142238、accent #2563EB、白色主区、冷灰边框、8px 常用圆角；字体 PingFang SC/system sans，正文 14–15px、标题 20px、辅助 12px。左栏约 230px，中心 flex 主区，右画布约 360–460px；默认无产物时收起，生成报告可展开。桌面独立滚动消息区域，输入框保持可用；窄屏画布以可关闭面板显示，避免被报告推到底部。

导航包括运维工作台、告警事件、Skill 管理、MCP 管理、模型设置、调用记录；底部为用户头像/名称/角色和退出，admin 额外账号管理。中间上下文标签与可编辑面板，消息含真实步骤摘要、报告卡片、可定位引用。画布保留真实报告、原始证据和执行记录；可信结构化指标存在才显示，不能从模型正文猜值或复刻示例常量。附件上传与下载关联服务端 metadata。所有建议追问先填输入框，由用户发送。

### 5. 归属与投影

run/evidence/invocation/events 的授权统一按父 run；AG-UI thread 查找、显式 business_run_id、binding、status、stop 均校验 owner，协议绑定键包含 owner。Node 仅转当前请求 Cookie，不缓存跨用户身份或信任 forwardedProps.owner。概览和队列详情过滤 owner；共享队列容量可统计但不显示他人目标、正文或 ID。

## Risks / Trade-offs

- [既有数据无 owner] → 默认仅归 admin，不改写证据原文；新 run 明确写 owner。
- [会话保留增加存储] → 维持服务器容量限制，附件限额，空间不足拒绝新增；本次不静默清理已保存历史。
- [附件为不可信输入] → 有界文本并标明用户材料，不执行文件，不把材料冒充源证据；未实现二进制提取明确标记。
- [同一消息部分受理或失败] → 先持久消息，记录执行收据/未执行原因；幂等重复返回原收据。
- [三栏在窄屏拥挤] → 保留 Chat 与输入框，画布可显式展开关闭；键盘与手机分别验证。

## Migration Plan

1. 在现有 Mac 项目与 PostgreSQL 上增量新增文档类型和字段，不清库、不修改 外部数据源。
2. 以现有独立口令引导 admin，旧无 owner 历史归 admin；保留登录兼容。
3. 编译 Web，执行受影响测试和两用户 PostgreSQL/浏览器验收，再仅重启本项目 Mac 进程。
4. 如回退代码，保留数据库与附件，不自动删除新会话；记录旧代码不支持的新数据边界。
5. OpenSpec 校验与规格对照完成后保留可审查 diff；GitHub 更新等待用户后续指令。
