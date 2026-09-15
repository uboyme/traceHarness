# WC-1D/E：必经 multi 职责分配

日期：2026-09-13。范围：[执行计划](../plan/TRACEHARNESS_WC1D_MULTI_EXECUTION_PLAN.md)、[ADR-0071](../adr/0071-required-multi-allocation.md)。保留开始前工作区改动；未提交、推送或发行。

## 实现与原因

旧 adaptive 可以合法选择 local，WC-1C 因此只有主方，未证明协作。现在默认 single 保持独立执行；明确 multi 必须经过最多两步侦察、一次 submit_collaboration_plan、一个只读助手完整交付，再由主方实施。没有动态 DAG、递归、并行或写助手扩权。

主方填写目标、交付和报告用途；助手填写目标、范围、禁止事项、交付和背景。所有字段连同宿主绑定身份、来源版本进入 work 2，预算、报告收集与评估共用 investigation_work Reader；真实助手请求检查这些字段，而非仅检查父方参数。

失败仍由原 Session/Supervisor/Workflow 记录。定位到原 Finish 的非 completed reason 仍可能形成 completed Delivery，因此不能只用 Finish 表示分工失败。CollaborationContinuation 产生具名异常；MultiAgentExecution 只在原执行返回非 completed 时转为失败，保留原 Budget runtime identity、Session 与 dispose。助手 report.status 和 reason 均 completed 才能继续；partial、申请额度、失败和超时均不能切回 single。

Product 5 / Product event 4 / host config 5 / comparison 3 / readonly work 2 拒绝旧合同；Session 14 / request view 1 / Context 12 / SQLite 2 不变。历史实验入口在加载连接前拒绝用当前源码重跑，原证据不迁移。AO 当前只允许修改 ALLOCATION_GUIDANCE；必经要求、程序门禁、权限和评分不在候选编辑范围。

## 验证

- 本轮已运行的定向及相邻检查，按最终结果合并去重为 **559 passed，零剩余失败/跳过**；不包含全量执行。
- 覆盖 Product/配置/Registry/Assembly/Session view/Tool/预算/评估比较/AO/TUI/历史合同拒绝，以及真实 Git、Docker、Supervisor 配合确定性模型的成功、失败与取消。
- 新增无计划和规划预算耗尽失败检查；缺工作字段不得创建助手；实际子请求包含完整职责；候选不能修改必经合同；新真实验收驱动有整树封顶与失败后停止检查。
- 反向验证在隔离源码副本移除完整报告门禁：助手失败后仍生成补丁、通过固定检查并到达 awaiting_approval，原定向测试明确失败。正式源码保留保护，同一公开路径随后通过。
- compileall 通过；4116 项仅收集；69 个本轮改动 Python 文件 Ruff 通过；两份上下文主章节对应、新增链接、围栏及 diff 检查通过。

过程中修正了旧脚本夹具仅允许父方自己的预算：新增子任务后须显式留下步骤、工具、Token 和墙钟额度；没有放宽生产预算规则。旧“multi 只有一个 Session”断言改为真实两份执行 Session。源码 revision 是合法子输入，不能因隔离 target 恰好从同一 revision 开始而误判为审批泄漏；Review、Promotion、目标 ref、新提交和固定验证内容仍检查不进入模型请求。

AO 在隔离 worker 的同模式比较通过，候选仍未自动采用。受保护的 AgentLoop/AgentRuntime/Supervisor/PluginManager 文件与本阶段开始前快照逐字一致；更新其旧测试摘要不包含本阶段修改。

## 边界与后续

结构合法不等于分工有用。提示要求实质交付、避免整题重复、不复述已知答案、明确报告用途；允许共同阅读同一源文件和必要核验，语义仍需看真实证据。WC-1F 另按一道新题、16 次调用、600 秒、零重试执行，结果单独记录；不能把这里的确定性模型测试当真实效果，也不能据此宣称优于 single。未运行全量、L2–L4、Wheel 或安装。

正式上下文与通俗版同步了当前状态、目录/owner、14.3 流程及图、14.3.1 协议与提示边界、15 验证；正式版当前 Product 合同/装配条目同步新模式。历史 DA 与 WC-1C 数字保持原合同含义。
