## Context

应用以 Store JSON 文档持久化配置与运行快照。源端共享身份不等于每用户可访问全部业务数据；配置管理员也不获得私人材料读取特权。

## Decisions

1. `access_policy/main` 使用 `source-access/v1`，包含版本和显式 grants/denies。每条规则绑定服务端主体、源、工具、目标、环境、目的、完整凭据引用集合和规范化目的地。配置角色不参与允许判断。
2. exact 参数范围通过 JSON Pointer 与固定目标/环境或固定值比较，不通过字符串替换重写查询。无法可靠收紧的工具只能在明确的全源授权下执行，否则拒绝。目的地允许 HTTPS；HTTP 只在策略明确登记的 loopback 目的地允许，重定向不得继承凭据。
3. attempt 保存完整策略快照与摘要；逐次派发同时通过冻结快照和最新策略，最新 deny 优先。返回材料在保存/披露前再次校验；撤权后的历史与派生材料按保存的来源范围重新授权。
4. `egress_policy/main` 使用 `model-egress/v1`，按模型、主体和材料类型登记字段路径/动作。未知类型和未登记字段失败关闭；凭据字段即便误列 allow 也禁止。日志、附件和源错误没有默认正文出站许可。
5. 随机别名按 owner + conversation 隔离，Store `egress_alias` 仅供服务端读取；alias 不编码原值。结构化身份字段用 alias 动作，文本只在明确登记的安全文本路径处理，源查询参数不经过出站变换。映射不进入正常客户端制品/日志/导出。
6. 出站快照固定可发送的类型/字段；每轮同时应用最新策略，最新禁止不能被旧材料或重试绕过。策略读取失败不回退旧版、不全量放行、不静默更换模型。

## Integration

独立 `access_policy.py` 不发网络请求、不导入应用入口、不读取 Vault。运行时/API/连接器在各自边界传入服务端身份、冻结配置、目标及真实参数。实际模型 HTTP 传输前最终检查覆盖 system/user/历史、工具描述/schema/返回、摘要、重试和异常；集成验收必须捕获真实请求字节。

### 实际 HTTP 边界

连接器将同步 request_observer 安装到实际 HTTP transport，初始化、工具目录、调用、流式会话结束、源登录、重试与分页逐请求检查；异步或抛错的 observer 不允许降级成无 guard 重试。冻结实际 URL 含路径和查询参数；同源不同路径也不能继承认证。transport 拒绝重定向响应，防止 SDK 自行跟随同源 Location 绕过客户端 follow_redirects=false。

模型调用在 SDK 序列化后的 HTTP 请求检查固定 provider/model、精确目的地、Host 和绑定认证 header。system、用户上下文、assistant/tool 协议消息、工具定义及 model_parameters 分开登记；extra_body 的未知字段不能混入已允许参数。准备文本的批准记录包含当前策略摘要，发送时策略收紧即不再有效；不依靠已准备消息或重试缓存继续发送。已撤销但本地仍保存的凭据也属于正文禁止值。源/模型网络错误使用固定公开文案，原始异常文本不作为保存报告或客户端错误内容。

### 历史与制品

运行材料保存 owner 和源 access_scopes，历史读取递归重验所有继承轮次；缺记录、循环引用或派生正文缺可信来源标记拒绝。来源未知的旧报告不能因为 owner 相同而升级为已授权。会话标题、正文、概览、队列与调用列表采用同一限制；生成报告保存、列出、metadata、阅读和下载均重验，下载另核对大小和摘要。用户原始 upload 保留 owner/会话边界，不自行变成源授权。

片段 API 从真实 readreceipt 重新执行原 selector，核对 run/attempt/owner、当前源范围和保存字节，结果摘要和 anchor 必须仍相同；不返回服务器路径。片段是否支持某条结论由 R04 再按 R03 的原始行号、投影字段和完整性判断，不能只因 anchor 存在就认可任意字段。

### 配置管理与选择入口

策略管理仅 admin，GET 返回 configured/revision/policy；PUT 使用 expected_revision CAS，版本内容不可改写。配置权不构成数据权。普通用户 settings 仅提供可见 default_model_id，Skill/模型只提供已发布启用的最少选择元数据；MCP 还需该主体显式最新 `$catalog`/discovery 授权。未授权源在列表和直接详情均隐藏，版本正文拒绝，概览不披露私有源名称或全局数量。新 identity_mapping/query_catalog 保存时验证版本合同，旧格式保留但不升级 ready。

## Risks

仅声明 run.target 而未约束工具参数不足以授权；任何自由查询必须由明确全源权限或可靠服务端查询目录/参数约束支持。任意文本不能自动识别全部业务敏感内容，未登记的日志/附件正文按默认拒绝处理。合成 SDK 捕获验证已知协议路径，不能代表所有 provider、源 schema 或未来导出形式。此变更仅定义未来 Case 逐读者授权，R13 的真实共享链路另验，不把它列为已实现 API。
