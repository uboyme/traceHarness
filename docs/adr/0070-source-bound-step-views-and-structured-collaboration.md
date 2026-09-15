# ADR-0070：来源冻结的 Step 视图与结构化只读协作

日期：2026-09-12。状态：WC-1B 实施；自然模型验收属于 WC-1C。

## 决定

采用 WC-1A 的 scout → decision → execute 合同。同一主 Agent、同一 Session；Product
根据事件解释阶段，通过 Runtime 的通用 StepViewPolicy 接缝选取请求工具子集、提示和输入模式。
AgentLoop 不解释 Product 角色，不创建另一个 Planner、Surface、任务状态表或调度器。

Session `context_protocol` 升为 14，旧 Session 明确拒绝，数据不迁移、不删除。新增原 Session
内的 `request/view` format 1：turn_id、step_id、source_seq、source_digest、base_composition、
label、tool_names、system_prompt、input_mode、max_calls。source_seq 指向紧邻前驱，摘要覆盖该
Session 的前缀；视图绑定当前 Step 与选定 Composition revision。RequestBuilder 和独立重放
使用相同 Reader。Context format 12、SQLite schema 2 保持不变。

surface 模式保留原 Surface；evidence 模式从当前 Turn 的 user/message 与 Surface 实际呈现的
tool/result 构造一条数据消息。每项包括原 stream/seq/step/call/effect 标识、状态、可见正文及摘要；
失败仍标失败，不引入隐藏全文、其他会话或模型执行叙述。原 Context 导航保留其导航身份。
实际发送、token 测量、Effect、RequestSnapshot 与恢复继续由既有 owner 负责。

ToolRuntime 在副作用之前检查整个批次是否属于冻结视图及调用数量。混合非法批次全部拒绝。
scout 最多两步、每步最多一次只读调用；decision 一次且仅 decide_task_collaboration；非法决定
结束，不给模型修复重试。execute 隐藏决定和调查管理入口，保留主方原执行工具。

local 不创建助手；delegate 复用 InvestigationControl 的身份检查、create/send、指定消息等待与
collect。报告正文和证据引用一起转交，不只转交机器元数据。等待上限 300 秒，原更短 Tool/Turn/
Task/Budget deadline 仍有效。取消/异常等待原 stop 与收敛完成后返回。失败、取消和预算申请报告
如实返回，不能称作业务完成；不自动拨款、续派或递归。Supervisor、工作区、预算与进程槽 owner
均保持原链路。资源不足按现有规则失败，不增加等待队列。

## 取舍与边界

相比自由 delegate/collect，当前版本每 Turn 最多一个调查助手、先收报告再执行，以较小状态空间
验证依赖式协作。single、Chat、调查助手与裁判不装配此策略。只读助手权限不扩大；不增加动态 DAG、
写助手或自动 Promotion。结构化执行成功不证明模型会合理选择 delegate；需 WC-1C 单独实测。
