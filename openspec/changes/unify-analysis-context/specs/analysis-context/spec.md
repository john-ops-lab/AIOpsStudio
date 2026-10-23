## ADDED Requirements

### Requirement: 冻结版本化对象身份
系统 SHALL 以 environment/cluster/namespace/type/id 的完整键表示对象，并冻结带来源、验证时间和已知有效期的版本化映射。显式 canonical 输入 MUST 由服务端 records 验证，不能由客户端自报原生映射。系统 MUST 对歧义、缺映射、同源 ID 冲突或未覆盖实际故障窗口的有效期返回 needs_input，保存澄清提示，且不执行源或模型请求。

#### Scenario: 同名跨环境与缺映射
- **WHEN** 相同名称出现在不同环境，或目标缺少明确映射
- **THEN** 范围内唯一且有效期覆盖故障窗口的完整键才可绑定；其他情况进入 needs_input，在 dispatch 前拒绝猜测目标

#### Scenario: 用户指定完整键但未登记来源
- **WHEN** 客户端提交完整 target_identity，但服务端没有对应有效 records
- **THEN** 不接受客户端原生 ID 或自行推导来源，保留缺映射提示

### Requirement: 固定窗口与显式查询绑定
系统 SHALL 在 attempt 冻结 UTC 绝对分析窗口、可选 baseline/change/validation 窗口、源偏差、查询目录内容和版本。工具 MUST 按显式目录绑定对象与时间参数；冲突、相对时间、未登记参数、目录或定义摘要不匹配必须拒绝。具名查询窗口 SHALL 再次核对映射有效期，基线可比性未知时不得推断为健康基线。

#### Scenario: 查询参数覆盖与源时钟偏差
- **WHEN** 模型提供冲突目标/窗口，或源需要已知时钟偏差
- **THEN** 冲突在 dispatch 前拒绝；合法偏差保存在 binding，源请求可还原同一平台窗口，未知偏差不按零冒充已知

#### Scenario: 目录未登记
- **WHEN** 对象身份已确定，但对应工具没有已审核目录条目或工具摘要已变化
- **THEN** 工具绑定拒绝，不按参数名称猜测；身份 ready 不等于工具可查询

### Requirement: 查询范围与返回对象分别核验
系统 SHALL 将查询 canonical_key 作为 query_scope；只有冻结 result_identity 明确版本、响应集合位置、逐行身份 pointer、native_id 或 canonical.id 映射，且 environment/cluster/namespace/type 四维通过 scope_fields 或经审核 fixed_scope 全部精确匹配，才可核验实际返回对象。系统 MUST NOT 根据标签名称、查询目标或 singleton 文档身份猜测任意返回行的对象。

#### Scenario: 多序列返回无关对象
- **WHEN** 查询目标正确，但某条返回序列的身份不同、范围缺失或未配置明确响应身份规则
- **THEN** 原始值仍可保留为有限保存材料；fact.object=null、verification.object=unknown，不能确认该目标正常或异常

#### Scenario: 明确逐行映射
- **WHEN** claim pointer 定位的实际行身份与冻结 mapping 一致，且四维来源范围完整匹配
- **THEN** 只核验该行的返回对象，其他行仍须分别核对，不把查询范围升级为整个响应的身份结论

### Requirement: 保留覆盖与历史未知
系统 SHALL 区分 complete、empty、forbidden、failed、stale、partial、historical_unsupported、unknown，保留分页、截断、越界和发生/观察时间边界。系统 MUST 不把当前拓扑当作历史事实，不把旧 run 自动认作新契约完整记录。topology 没有显式时间参数时，即使目录 historical=true，其历史支持也 MUST 降为未知。

#### Scenario: 成功空结果与历史拓扑
- **WHEN** 查询成功为空、失败、分页未完或源无历史拓扑
- **THEN** 结果保留各自状态；未知历史为 unknown、明确不支持为 historical_unsupported，均不能说明故障时拓扑

#### Scenario: 声明历史支持但只查询当前快照
- **WHEN** topology 目录声明历史支持但没有显式时间绑定
- **THEN** binding.historical_support=null，历史 coverage 为 unknown，不根据 valid_from/valid_to 名称推断工具支持

#### Scenario: 旧配置与旧尝试
- **WHEN** 读取没有新上下文版本或有效映射目录的历史数据
- **THEN** 保留 legacy_unverified/unknown，不填充新完整标志或重写旧快照

#### Scenario: Successful response includes incomplete coverage
- **WHEN** a successful Prometheus response explicitly reports partial, failed, forbidden, stale, unknown, incomplete pagination, truncation or a continuation marker
- **THEN** runtime coverage SHALL preserve or conservatively degrade that limitation and MUST NOT replace it with complete/empty; structured claims cannot become verified by the success envelope alone

### Requirement: 对外上下文只返回已选范围
系统 SHALL 将完整身份映射、查询目录和源偏差保留在服务端冻结快照，并在对外运行详情移除 query_catalog、source_offsets、identity.mappings/candidates、配置/策略快照和后端路径。当前对象、窗口、版本/摘要、状态与澄清缺口可按 owner 和最新来源授权返回；管理员配置入口不授予读取他人历史的权限。

#### Scenario: 普通用户查看运行详情
- **WHEN** 已授权 owner 读取某次分析
- **THEN** 页面获得当前对象和窗口的可解释状态，不获得全局目录、其他来源映射或凭据；HTTP 请求不能修改已冻结上下文
