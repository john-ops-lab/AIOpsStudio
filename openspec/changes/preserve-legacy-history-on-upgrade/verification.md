# PR #15 upgrade archive verification

## Observed issue

M1严格来源契约使旧无scope记录不可读取，进而隐藏会话及原生成报告附件。已由独立审查与隔离API fixture复现，修复前旧会话详情404。

## Local verification

- 显式计划/注册只增加一个私有清单文档；原run、会话、附件、settings、user行与附件bytes前后相同。合法owner列表/详情/原文件下载可读，另一管理员读取仍404。
- 重试、发消息、关联新run、材料补读、历史继承、上传及旧材料阅读上下文拒绝；未登记伪旧记录拒绝。
- 计划后变化、清单扩容、登记后内容变更拒绝；保留期清理不会删除登记档案。
- 独立交叉审查发现foreign conversation/thread ID先返回档案409，已调整为先owner校验；新增conversation/thread/parent三个入口检查均404、不含档案提示。
- 新档案四个测试与源在途撤权、过期重试回归联合运行：6 passed。此前Chat/授权/AGUI/归属相关68项通过；不累加重复执行计数，最终全量结果由主集成登记。
- 前端构建通过（已有大chunk提示保留）；工作台档案展示说明、保留原消息/附件并引导新建会话。新change严格校验通过；完整校验/真实Mac升级核验由主集成登记。

## First failures and boundary

扩展验证发现原API测试adapter未接受新增只读参数，修复fixture签名后通过。新增source invocation复验最初误将model invocation当作源授权，已限定真实source invocation；过期清理随后丢失授权元数据导致原410变404，已保留owner与scope（对外仍移除scope），回归通过。

此验证仅覆盖隔离合成数据库及本机代码。实际已有私有记录清单、备份、原行/文件核验和切换结果不进入Git；不能据此声称部署或MiniMax端到端已通过。
