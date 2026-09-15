# ADR-0071：multi 必经职责分配

日期：2026-09-12。状态：WC-1D 冻结，WC-1E 实施依据。替代 ADR-0070 中 Product Adaptive 可选择 local 的合同；保留其通用来源视图、原 Supervisor 和所有权设计。

## 决定

当前 Product 模式切换为 single/multi，默认 single。multi 同一主 Agent 最多两步只读侦察，随后一次 submit_collaboration_plan，必填 main_work 与 child。取消 local 与旧决定工具；首版恰好一个只读助手，宿主等待确切消息的完成报告后主方才能实施。分工字段与五段提示采用 [WC-1D 计划](../plan/TRACEHARNESS_WC1D_MULTI_EXECUTION_PLAN.md)第 4、5 节。

Product 拥有阶段规则，Runtime 仍只承载通用 request/view。计划参数保留在原 Tool call；原 InvestigationControl 写入完整分工工作信封，原 Inbox/Delivery、Session/Effect 保留唯一事实。统一信封校验由 supervision 的工作协议模块拥有，预算读取、报告与 Evaluation 复用，不能分别猜测版本。没有新 Planner、调度器、图 DSL 或业务状态表。

## 版本切换

| Owner | 原版 → 当前版 | 原因 |
|---|---|---|
| Product protocol | 4 → 5 | 模式语义及装配身份改变 |
| Product event schema | 3 → 4 | 防止旧 fixed multi 或 adaptive 事件被重解释 |
| Product host config | 4 → 5 | 当前配置仅接受 single/multi |
| Evaluation comparison | 2 → 3 | single/multi 新执行策略合同 |
| readonly-investigation 工作信封 | 1 → 2 | 必填范围、排除项和主方职责 |
| Session / request-view | 14 / 1 保持 | 通用来源冻结已足够表达新工具和提示 |
| Context / SQLite | 12 / 2 保持 | 没有改变对应数据结构 |

旧格式/模式明确拒绝、数据保留，无自动迁移、别名或双 reader。历史实验必须使用其冻结源码。Profile 自身的 profile_version 是宿主内容版本，不等同以上持久协议；当前生成的内置配置更新该内容版本。新运行的配置、manifest/dataset 摘要须按真实字节重算。

## 工作信封和报告

format 2 的 readonly-investigation 信封包含 kind、owner_agent_id、source_id、revision、goal、scope、exclusions、deliverable、briefing、main_work、input_digest。main_work 必填 goal/deliverable/uses_child_report。所有字段严格校验，文本限长沿计划，input_digest 覆盖除自身外的整个信封。字段是职责约束，不授予额外工具权限或逐文件 ACL。

submit_collaboration_plan 复用原 delegate、wait_message、collect；返回 plan、child 和 outcome。只有真实报告 status=completed 且 reason=completed 才 outcome=completed，否则 child_incomplete。失败/取消/预算申请报告原样保留，multi 不进入后续实施。取消和等待错误沿原 stop/cleanup 收敛，不吞原异常。

Continuation 从当前 Step 的真实 Tool result（含受保留的大结果）读取 outcome，未提交/非法/失败/不完整分别以有稳定类型的异常进入原 runtime/error 与 Delivery failed 路径（仅 Finish 不保证失败），预算停止也不能作为 multi 正常完成。Product 观察从原任务消息对应的 Session 读取停止原因，展示具体阶段；不把模型说明当宿主确认原因。

## 提示与受限优化

主方必须分工和原权限规则属于不可优化的合同说明；规划语义建议单独为 ALLOCATION_GUIDANCE，允许原 Evaluation 文本候选编辑这一真实可见说明。删除当前候选白名单中四个已不出现在 multi 请求的自由委派说明；旧候选因源码/选择器不符明确拒绝。裁判、完成门禁、权限和预算不进入可编辑面。

## 验证和限制

结构/权限/身份/单次派发/失败收敛为硬规则；分工有用、非重复及报告使用由冻结轨迹语义核读判定，不能使用名字、关键词或路径差异冒充判断。single 保持原行为；助手仍只读、不并行扩展、不递归。WC-1E 定向与反向验证通过后执行一次获准的 WC-1F，最多 16 次真实调用、600 秒、无重试。全量、L2–L4、提交与发行不属于本轮。
