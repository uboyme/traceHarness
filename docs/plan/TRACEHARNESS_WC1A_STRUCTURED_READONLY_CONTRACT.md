# WC-1A：结构化只读协作合同

日期：2026-09-12。状态：调研及设计冻结完成，WC-1B 尚未实现。没有测试或真实模型成绩。本合同细化 [WC 总计划](TRACEHARNESS_WRITABLE_COLLABORATION_PLAN.md)的 4.1，不引入可写助手或动态 DAG。

## 1. 旧实验与本次可验证差异

| 阶段 | 已有证据 | 不可重复包装成新能力 |
|---|---|---|
| DA-7/8 | 合法 separable 在原 Tool Effect 内直接调用 delegate owner；DA-8 首轮创建过真实助手 | 程序派发已经做过，不是模型还要再调用一次 delegate |
| DA-8 | 首轮派出后未 collect，第二轮可拆题选 local；候选撤回 | 创建成功不能算闭环，不复活独占首步全部候选 |
| DA-9 | 少量侦察后切换决定，两个复杂题仍请求读取，候选撤回 | 仅插入“现在决定”提示没有解决输入切换 |
| DA-11 | 目标与原工具结果重组成独立证据包，两种条件各 3/3 合法决定；未执行工具 | 可以借鉴输入形态，不把离线 probe 接成绕过 Runtime 的 Provider 调用 |
| DA-12/13 | 旧独立分工标准过窄；后续接受先调查再分析，但自由委派仍未出现 | 不再要求主方与助手同时各完成独立结果 |
| DA-14 | 主方明确职责已送达，四题仍零委派 | 暂停继续加长自由委派提示 |

本轮合并假设：**有界侦察 → 独立证据输入中的结构化决定 → 宿主派发并等待绑定报告 → 主方基于结果完成任务**。首次只测串行协作，不主张并行提速。仍可能选 local、提出不合理分工或误用证据；这些是需要如实记录的失败，不由程序伪造语义判断补齐。

历史来源：[DA-7](TRACEHARNESS_DA7_TYPED_DECOMPOSITION_CONTRACT.md)、[DA-8](../deal/034-exclusive-adaptive-decomposition.md)、[DA-9](../deal/035-scout-before-decomposition.md)、[DA-11](../deal/037-independent-decision-input.md)。历史合同、分数和撤回决定不改写。

## 2. 触发与阶段规则

仅 Product 已确认的 Adaptive 主方进入本合同；single、普通 Chat、只读助手、AO 裁判和优化调用不进入。默认 single 不变。每个执行消息最多一个决定、一个初始只读助手，禁止递归及本轮继续创建其他助手。

阶段从当前执行消息关联的 Session/Effect 事实重建，不依赖可变 phase/counter 或 Product 新状态表：

| 阶段 | 可见能力与动作 | 转移 |
|---|---|---|
| scout | 最多两个模型 Step；每 Step 最多一次原 list_files/read_file/search_text，只读与真实权限交集 | 两步结束即决定；若模型提前无工具回复则提前进入决定，不能直接宣称任务完成 |
| decision | 仅公开 `decide_task_collaboration`；独立目标/证据输入 | 一次合法 local 进入执行；一次合法 delegate 在工具 owner 内派发并有界等待 |
| execute | 原主方读写/验证能力；显示决定结果及子方报告，移除决定及直接创建/续派/额度决定入口 | 依原预算、重复拒绝、验证、完成和取消规则收口 |

scout 的两步按实际尝试计数，失败读取不能无限延长；单次响应多个工具调用整体拒绝，不能先执行一部分。decision 只有一次模型机会，不加漏答修复轮次；自然语言回答、非法参数、非决定工具或混合 batch 均留下失败并停止本次试验。主方第一次 execute 之后不允许回到 decision。max Steps/Token/时间不足时优先停止，不为进入下一阶段补预算。

这些是首版受控实验合同，不保证两步足够理解所有任务。题目若不能在冻结预算下给出有意义决定，应记录不足，不能按主题强派助手。用户明确要求使用助手的验收题若选 local，仍按合法 local 执行，但协作验收为未触发。

## 3. 决定输入与输出

### 3.1 独立输入

继续使用同一个主 Agent、Session、模型和原 LLM Runtime，不新增 Planner Session，不从 Product 直接调用 Provider。只对 decision Step 使用专门的请求视图：

- 当前真实任务目标、原主执行消息身份及精确来源版本；目标是待分析工作，不能伪装为本 Step 必须立即执行读写的指令。
- 实际已送达主方请求的宿主导航，标记 navigation；不能把导航升格为正文。
- scout 成功工具结果的已呈现正文及原 stream/seq/step/call/effect 引用、正文摘要；取原可见范围，不偷偷补完整文件、未来结果或其他 Session 内容。失败记录单列状态，不作为成功证据。
- 当前宿主已授权的只读能力边界与预算约束；不暴露秘密或任意路径配置。
- 本 Step 指令：仅提交 local/delegate；可以先调查后分析，不要求并行；原文是证据数据，不是权限或系统指令。

不保留 scout 的执行叙述和末尾“继续实施”提醒作为 decision 的对话指令；其原事件完整保留。工具调用/结果整组转为有来源的数据，不产生孤立 tool 消息；没有 LLM 摘要或改写正文。execute 恢复原 Surface 主线并加入真实决定结果，不删除历史。

### 3.2 输出 schema（冻结语义）

仅一个工具调用，所有键必填，additionalProperties=false：

| 字段 | 类型与限制 | 含义 |
|---|---|---|
| action | local 或 delegate | 不强制选择 delegate |
| reason | 非空字符串，最多 2000 字符 | 为什么这样组织工作，不能作为权限依据 |
| main_work | 非空字符串，最多 4000 字符 | 主方接下来如何完成任务，可依赖助手报告 |
| child | local 时 null；delegate 时严格对象 | 只包含 goal、deliverable、briefing |
| child.goal | 非空，最多 4000 字符 | 本次有界只读调查目标 |
| child.deliverable | 非空，最多 2000 字符 | 要交的证据与结论 |
| child.briefing | 非空，最多 8000 字符 | 必要背景及明确未知；沿原调查输入限制 |

没有 agent_id、工作区路径、工具列表、token 增量、模型名称、DAG 边或是否需要审批字段。身份和能力由宿主绑定；字段合法只证明可执行格式，不证明分工合理。不给 schema 增加“与主方目标文本不相同即有效”的伪语义评分。

## 4. 派发与报告回收

`decide_task_collaboration` 是一个 EXTERNAL_TRANSACTION 工具：local 返回真实决定回执且不创建；delegate 复用原调查控制 owner 的 create/send 身份规则，然后在同一个受管工具执行中等待这个 child/message，读取原 report、预算申请与来源证明，并返回有界结果。首版主方在此期间等待，没有后台轮询 Agent。

必须先沿原预算/并发准入判断是否可运行；父方持有运行槽时不自动释放或增设槽。容量不足拒绝，不能派出一个永远等父方释放槽的助手后无限等待。即使有准入竞争，也受下述等待上限约束。

等待最长 300 秒，并同时受主方/助手现有 Turn、Tool、任务 wall 与预算限制，取先到者；这不是修改生产默认超时。原 Supervisor.wait_message 使用生命周期通知，不用模型反复 collect。等待结束沿原 collect 逻辑核对 owner、message/source、Session、revision 与正文身份。当前 collect 本身最多等 30 秒；实现不得简单传 300 绕过参数合同，应由同一协作 owner 复用原 wait_message 后作零等待的报告读取。

正常返回：决定身份、实际 agent_id/message_id、真实 terminal status/reason、报告引用和按现有工具结果预算呈现的正文。大报告继续沿原 retained output/读取机制；只能声称引用/预览到达，不能假定全文已送达。原 RequestSnapshot 证明下一主方请求实际看到了什么。

failed/cancelled/额度申请结束均返回真实状态和可用的部分证据，不伪装 completed investigation，不自动拨款/续派。execute 不开放额度决定和续派，本轮能力更窄；原非本合同调用入口的额度机制不被删除。主方可以按证据完成或说明阻塞，但不能声称助手已完成未完成的工作。

超时或宿主取消：中断并等待该子树原 cleanup 收敛，再返回失败/传播取消；取消不能被转成正常报告。创建成功但发送失败、报告读取失败或结果持久化失败也必须收敛活动任务并保留原错误。无需为读取成功报告立即释放全部资源，最终工作区仍由原 Product 资源 owner 收尾，先收敛再主方 capture。

## 5. 重复、身份与事实源

决定绑定 task/main-agent/main-message/session/turn/step/tool-call 及请求快照摘要；子创建与 message_id 使用原确定性操作身份。一次响应多个决定调用整体拒绝，防止同一 Step 双派发。

同一决定执行实例内已创建的身份可用栈上局部变量管理 cleanup，但状态解释仍回到 Directory/Inbox/Delivery/Effect；不引入 decision_db、report_cache 或 applied 列表。再次用新 call_id 提交决定明确拒绝；同一已结算 operation 的观察读取原回执，不重复 create/send。

外部 Effect 的 unknown/未结算不是“没执行”：不重试整个事务来猜测，沿原日志查已创建身份并保留失败/未证明结果。当前不增加进程崩溃后的自动接管承诺。正式的跨进程恢复若需要新协议应另立阶段，不借本轮顺手实现。

## 6. 实现 owner 与必要接缝

| 所属层 | WC-1B 允许的最小改动 | 禁止 |
|---|---|---|
| Product assembly/runtime | 仅 Adaptive 注入本策略与阶段说明，single/child 保持原装配 | 新 Product 状态机或固定 Planner Agent |
| 原协作 owner | 将现有 create/send、报告校验、stop/cleanup 提取为窄复用操作；结构化工具组合调用 | 跨模块调用私有 `_InvestigationControl`、复制 Supervisor 或复制报告格式 |
| Composition / RequestBuilder | 可选的通用 Step 请求视图接缝；冻结工具子集、消息视图及来源并统一计量/重建 | Provider 前临时替换 request、保存 mutable messages、硬编码 local/delegate 业务 |
| Session 请求证据 | 新视图记录及 parser/version、独立请求重建；记录视图所依赖的真实事件/头位置 | 仅保存一个不核验来源的最终 prompt 或第二 Surface 事实源 |
| ToolRuntime / Policy | 验证本 Step 冻结可见工具及 batch 准入；决定结束前无普通写入 | 只隐藏 UI schema 却允许执行隐藏工具 |
| Continuation | 原 Budget/取消/失败优先；scout/decision 不能被普通结束消息跳过 | 替换原停止条件、无限补提示或偷偷调用模型 |

当前 RequestBuilder 没有独立 decision 视图接缝，当前 Runtime 也没有有效的撤回候选阶段接线。这些需要实施，不是只改一段 Prompt。视图使用原 Session 流中一种版本化请求输入记录（具体公开字段列入 WC-1B 实施 ADR）；请求重放必须从它的来源事件重建并核对正文、工具子集、组合摘要和 request fingerprint。新字段不得塞进未校验 metadata 绕过协议；若现有 Session 请求协议不能表达，按 pre-1.0 升版拒绝旧数据，不静默迁移。

阶段选择归 Product 策略，从原执行证据派生；Runtime 通用接缝仅承载已冻结视图。WC-1B 开始先写接口/协议 ADR 和针对请求重建的反例，再接生产。若接入需要第二生命周期或未经来源核验的派生状态，应退回本合同，而不是靠放宽测试通过。

## 7. 验证与采用标准

离线及定向必须覆盖：single/child 请求不变；scout 零/一/两步和失败；local 零创建；delegate 一次派出；混合 batch/重复/非法决定零额外执行；错误 owner/消息/正文/未来证据拒绝；隐藏工具零执行；成功/失败/额度申请报告实际进入下一请求；父子槽不足；超时、重复取消、创建/发送部分失败、结果 unknown；整树收敛和主方最终 capture。关键保护做反向测试，原请求重放、预算、Workspace 和 Effect 相邻回归不能跳过。

WC-1C 沿总计划只做一个明确要求只读协作的完整真实任务：两方真实模型、固定模型/Provider、合计最多 16 次调用含 scout/decision/execute/助手，600 秒任务上限、60 秒连接超时、无重试。不追加分类基线、裁判或多题追分；非委派/负面分支先由确定性测试覆盖，不能因此宣称真实模型分类稳定。材料在实施完成后冻结，必须有助手提供必要证据、主方整合为可验证产物的实际工作，不能只复述给定答案。

采用条件分开记录：①机制/身份/取消与重放门禁通过；②一次真实决定合法且派发一次；③真实助手完成并报告进入主方请求；④主方使用相关证据；⑤原 Product completed 与固定验证通过；⑥预算/工作区收敛、费用如实记录。任何一项缺失都不能宣布只读闭环验收通过或进入 WC-2 扩权。

真实不足时保留失败，不改题、提高上限、强填决定或再加提示重跑。WC-1B 作为有界候选验证，未通过 WC-1C 不将其宣称为新的默认生产策略；如何保留或撤回需依据实际失败范围，不能恢复旧字段兼容或同时维护两套派发逻辑。用户未授权发布，本轮无提交/发行；禁止全量、L2–L4、Wheel、联网安装。

## 8. WC-1A 交付

本合同明确了新旧差异、三段触发规则、独立请求证据、输出字段、同一工具内派发/等待、300 秒等待与原预算优先、原调度 owner、协议接缝、失败和验收。具体实现 ADR 的序列化字段须在 WC-1B 编码前细化，本轮不假称已实现。

完成项仅为源码/历史记录调研、合同与两份上下文同步、文档链接/编号/围栏与 diff 检查。未运行代码测试、Docker、真实模型或提交。下一步是 WC-1B 的接口 ADR 与最小实现，不开始 WC-2 可写助手。
