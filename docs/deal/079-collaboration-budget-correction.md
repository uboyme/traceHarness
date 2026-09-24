# 079：真实仓库评测的协作额度纠偏

2026-09-20。承接[记录 078](078-real-model-repository-pilot.md)与[执行计划](../plan/TRACEHARNESS_REAL_REPOSITORY_EVALUATION_PLAN.md)。本记录区分既有实测、代码修复和随后逐个冻结的可行性试次。

## 已验证的问题

旧试跑把 single/multi 同锁在整树 800,000、coder 760,000 token；multi 最多两名只读助手各初始 160,000，主方须保留 200,000。六次 multi 全失败，其中部分在分工前或后续模型准入耗尽额度；astroid 第一轮仅分工阶段已消费 595,043 exact token，第二轮的助手和主方又各自触及容量。三次部分派发失败取消了已启动助手，取消时的 Provider usage 不可精确补回。旧批次原结果与分母不改。

旧 v3 两臂合计已知价格估算 10.529522 元，三个未知取消调用另占规划 0.708816 元；加上 v1 诊断，操作规划 16.590714/100 元。它不是供应商账单，且不能证明真实费用的数学上界；但足以说明 **80 万 token 的单试次配置先成为瓶颈，整批人民币规划仍有空间**。把这批 multi 失败直接归为策略能力差没有根据。

`CollaborationPlanTool._require_capacity` 原先比较了整批助手初始 tokens、steps、tools、wall 与 `ledger.available()`，却遗漏每个 child grant 在真实 Ledger 中还要求的 `retained_tokens`。所以合计授权看似装得下，逐个 dispatch 时第二个助手仍可能被保留额度拒绝；原 `_converge` 随后按正确生命周期停掉第一个。根因在协作计划的可纠正预检，不在 Ledger 的容量不变量；不能放宽 Ledger 来“通过”。

## 本次修改

计划预检现在要求整批实际初始 token 授予之和加上本批最大的 retained_tokens 不超过主方可用余额；若不能满足，在创建助手前给出可纠正错误和数值。没有声明 grant 时仍由原逐个 Ledger 准入决定。真实 reserve/commit、Session、Workspace、取消和所有权不变。测试构造 400,000 主方余额、两名助手各生命周期 250,000/初始 160,000、主方保留 100,000：单名可行，双名的 `320000+100000>400000` 在预检拒绝，Directory 未增加身份且余额未动。暂时去掉新比较，新测试按预期因未拒绝失败，恢复后通过。

## 实验范围与已执行试次

撤回 10 开发 + 30 留出 × 三次 × 双模式的 240 次本阶段草案。12 道留出任务配对尚未执行；用户随后将当前目标进一步收紧为 **先让一题真实仓库 multi 完整跑通原 Runner/Evaluator**。扩集、配对统计、软质量新评分和有界优化均暂缓。所有下面的条件只在已准入的 astroid 开发题上诊断链路，不能作为模式收益样本。

v5 调试脚本误从旧 `.traceh/rr-eval/final-input` 复制材料，并在预登记中自称 material version 3；离线原始/参考准入当时通过，却未检查旧 verifier payload 的 `protected` 与 `test_files` 是否重叠。新条件把整树额度提高到 180 万、coder 170 万、两名只读助手各 30 万、主方保留 40 万。首次真实调用经 Windows 系统代理遭 `tls_eof`，保留为 `.traceh/f5a`；同条件设置进程局部 `NO_PROXY=dashscope.aliyuncs.com` 的 `.traceh/f5b` 则没有 Provider 错误，但两名助手和主方均在后续请求遭 `BudgetExhaustedError`，Product/Workflow 失败。原报告与 59 份请求快照已独立重读；账户、Workspace 均收敛。

v6 在独立冻结目录把整树额度提高到 500 万、coder 480 万、两名助手各 120 万、墙钟各 600 秒，主方保留 60 万；直连的 `.traceh/f6a` 无 Provider 错误。一名助手在 18 次模型调用后完成，另一名在约 626 秒工作时长被取消，主方因此无法收集全部报告。82 次模型尝试、Product/Workflow 失败，没有进入固定 Verifier；84 份请求快照独立重读，4 个预算账户关闭、4 个 Workspace 不再 live。CLI 的 `report.complete=true` 只表示测量完整，不能当成任务成功。

v7 在相同材料上改为最多一名只读助手、助手累计 200 万 token 和 1500 秒墙钟；主方仍为 480 万、整树 500 万，调用前冻结于 `.traceh/rr-eval/feasibility-v7/preregistration.json`。`.traceh/f7a` 的模型和工具调用没有 Provider 错误，但 coder 单 Turn 预留后只余 600 秒可分配墙钟，计划中的 1500 秒 child grant 无法装入；五次 `submit_collaboration_plan` 均失败（首两次还分别有字段错误和请求两名助手），没有一个助手真正派发。主方最终触发 `collaboration-plan-required-before-completion`；Product/Workflow 失败、无固定 Verifier。报告 `complete=true`、20 次模型尝试、2 个账户关闭、2 个 Workspace 释放；22 份请求快照独立重读。

v8 保留单助手和 200 万 token，只把助手与单 Turn 墙钟改为 600 秒；其它材料和主方资源保持 v7 条件，冻结于 `.traceh/rr-eval/feasibility-v8/preregistration.json`。`.traceh/f8a` 中模型先要求两名助手被宿主拒绝，随后在等待时限拒绝提示后改用 `dispatch_and_continue` 成功启动一名助手。助手 20 次模型调用后用满 600 秒墙钟而取消，主方 46 次模型调用完成自己的 Turn，但收集到的不是已完成助手报告；Product/Workflow 失败，固定 Verifier 未运行。66 次模型尝试无 Provider 错误，68 份请求快照独立重读，3 个账户和 3 个 Workspace 均收敛。

这也澄清 v7 的时钟原因：coder 的 420 万毫秒累计墙钟中，单 Turn 预留 360 万后，Ledger 当前可授予助手的仅剩 60 万；150 万 child grant 因此在容量预检被拒绝。同步工具自身约 55 秒的等待限制是另一层约束，v8 通过 `dispatch_and_continue` 跨过了这一层。v9 同一题保持单助手/200 万 token，把任务累计墙钟设为 600 万、coder 累计 540 万、coder 单 Turn 300 万、助手 150 万毫秒，让 child grant 能装入原 Ledger，长等待仍交给异步收集；冻结于 `.traceh/rr-eval/feasibility-v9/preregistration.json`，输出 `.traceh/f9a`。

v9 一名助手在 32 次模型调用后成功完成并被主方收集，主方 54 次模型调用后交回源码及回归测试补丁；无 Provider 错误。固定 Verifier 在真正执行断言前以 `protected test material changed: tests/unittest_python3.py` 拒绝，因此原 Product/Workflow 失败、Review failed、无 Promotion；86 次执行模型调用、88 份请求快照独立重读，3 个账户和 Workspace 收敛。原报告不得重算为成功。

根因不是当前通用材料生产者：v5–v9 沿用的旧 `final-input` payload 把 `tests/unittest_python3.py` 同时列入 `protected` 和运行时会完整覆盖的 `test_files`，而当前 `benchmarks/real_repository_v1/selection.json` 已显式声明普通候选测试可写，`tests.real_repository_evaluation.materials` 会排除重叠。重新从当前 selection、已缓存的原始上游归档和生产者生成 `.traceh/rr-eval/material-v3-correct`，三题均无 `protected`/`test_files` 重叠。把 v9 的原源码、ChangeLog 和新增回归测试字节放在这一正确材料的独立候选副本，原冻结测试 **26/26 通过、exit 0**，证据在 `.traceh/cf-f9-v3`；它只是反事实诊断，不能修改 v9 的原报告。

v10 从上述正确材料仅抽取同一 astroid 题，保留 v9 的同一资源配置、真实模型、原题需求和原 Runner/Evaluator，调用前冻结在 `.traceh/rr-eval/feasibility-v10/preregistration.json`。新材料离线原始故障 exit 1、参考修复 exit 0，均 admitted；真实输出单独写 `.traceh/f10a`。一名助手完成并被主方收集，原 Product 与 Workflow 均 `completed`，固定 `upstream-tests` exit 0 / Review passed，Promotion 回执的 new revision 与目标 ref 现值同为 `32e4489340ebe4b18586a000dcbbd8fa0223e9d1`。公共报告 `complete=true`、该 attempt `success=true`；79 次执行模型调用、精确 3,132,713 token，无 Provider 失败，3 个预算账户关闭、3 个 Workspace 释放且 live=0。独立重读 81 份请求快照，原数据库不变，目标与证据重导一致，见 `.traceh/rr-eval/feasibility-v10/recheck.json`。v5–v9 仍保留且不与 v10 拼接成功率；这一题是开发集链路验收，不证明 multi 相对 single 更好。没有为这些试次修改通用 Provider、Runner、Ledger 或 AgentLoop。

本轮只做单题可行性，不启动之前拟议的 120 元配对阶段门禁。失败、取消和未知 usage 分开保留；不把 `report.complete`、模型回复完成或主方成功错写成 Product/Workflow/Verifier/Review/Promotion 全部通过。

## v10 上下文与助手交付复核

用户追问 token 消耗后，离线读取原 `model/attempt-end`、`request/snapshot`、工具回执和冻结源码。
[诊断摘要](../validation-data/real-repository-pilot-v1/feasibility-v10-context-audit.json)保存角色统计、来源序号、数据库摘要与源码一致性；本轮不调用模型、不改原运行事实。

| 角色 | 模型调用 | 输入 token（exact） | 输出 token（exact） | 首次 / 最后一次输入 |
|---|---:|---:|---:|---:|
| 主方 | 46 | 1,885,301 | 70,706 | 5,087 / 65,166 |
| 只读调查助手 | 33 | 1,115,140 | 61,566 | 4,638 / 67,984 |

总量 3,132,713 中输入占 95.78%。两方各只有一个 Turn，输入从未下降，均无 `surface/replace`、
`summary/input` 或 `request/token-measurement`。按 cl100k_base 对冻结请求组件离线估算，工具结果占累计
输入的约 77.5%，其中 read_file 是主要来源；这是组成估算，不是 Provider 精确分项或已测节省率。
一份主方文件清单约 2,739 个本地估算 token，被重复带入 32 次请求。

已核对当前相关源码与运行时冻结源码相同：ProductAgentRuntimeFactory 为主/子 Agent 构造 RuntimeConfig
时均未传 compaction、token_budget 或 semantic_summary；累计费用准入所用 token_estimate 不等于
完整请求窗口/压缩策略。现有 CompactionService 仅选择已闭合 Turn，tool_fold_plan 还要求旧结果已有
output_ref；本次 187 条工具结果均无 output_ref。因此只打开现有压缩开关，或只提高总预算，不能解决
当前单 Turn 内连续读代码的增长；Product 也没有装配保留结果的读回工具。主方 39 次、助手 48 次
read_file 存在调查重叠，但不能把所有必要重读都判作浪费。

**助手交付结论需更正**：助手最后一个 attempt（Session 序号 518）为 `finish_reason=length`，
报告输出 8,192 token；序号 517 的 assistant/message 正文为空且无工具调用，序号 520 却为
`turn/end.reason=completed`。主方两次 collect 的 `statement` 均为空，助手仍有 823,294 token
生命周期余额。主方最终回答也明确说明助手未提供可用发现，修复依据来自自己的检查。
因此前文“助手完成并被收集”只表示生命周期状态和收集回执，不能描述成已交付有效报告。
Product/Workflow、固定测试和 Promotion 的原成功事实不变；本题不构成有效协作贡献证据。

源码根因在普通响应完成判定：InvestigationBudgetContinuation 在没有预算申请时委托
DefaultContinuationRuntime，后者在无工具调用/失败验证时不检查 finish_reason 就返回 completed；
AgentRunReportReader 忠实读取该状态，协作收集门禁只检查 status/reason。离线调用现有公开 continuation
入口，以空正文、length、step=33/max_steps=100 复现 `Finish(completed)`。这是实际路径缺口，尚未修复。
现有 usage 不单列该调用的推理 token，不能把 8,192 全部断言为已保存或可读的推理正文。

后续优先核对截断/空报告的完成语义，再设计单 Turn 内已闭合工具组的有界可见历史与原证据读回。
上述仅为诊断和候选方向，不表示已改变 Session 协议、压缩粒度或现行门禁。
后续已按用户要求写成[方案 080](080-long-task-context-and-completion-design.md)，明确根因 owner、
协议与权限边界、实施顺序和最小验收；该方案尚未实施，不新增本记录的真实运行或修复成绩。

## 验证与未运行

派发前预检的定向正反和取消/相邻测试、反向验证、compileall、collect-only、Ruff、diff check 已通过。v5–v10 已调用真实 Provider，v10 原报告、固定 Verifier 回执与独立证据重读一致；没有新增可运行留出题。完整全量和发布门禁未运行。不提交或推送。
