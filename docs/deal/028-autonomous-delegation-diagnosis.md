# 自主委派诊断：调用链可用，有效交接尚未通过

日期：2026-09-11。已完成原证据核对和 12 个有限真实任务；未采用候选，生产源码不变。
本轮不是新发布门禁，也不改写原 DA / 72 题成绩。冻结范围见[实验合同](../plan/TRACEHARNESS_DA_AUTONOMOUS_DELEGATION_DIAGNOSIS.md)。

## 1. 已核实的结果

旧机制/开发批次的 30 个 adaptive trial，共 276 份主方请求，四项委派工具始终可见，预算也已授予子任务空间；委派调用为零。实际任务材料仅有 2～16 行 Python。这组材料适合检查缩减后的合同，不足以判断真实仓库任务何时需要助手。必须排除无工具的 benchmark requester Session 后再检查主方请求，不能据 requester 错判工具没有装配。

新复杂题逐字节复制当前仓库 18 个模块：263,560 字节、6,919 行 Python。要求核对权限/源码版本、预算及取消/产物边界，交付带代码证据的 `answer.json`。明确委派和自然任务使用同一树和同一基本要求，只有前者额外要求找只读助手。简单题只读取版本源码并生成对应 JSON。材料、评分和预算在运行前冻结。

| 条件（每版各重复两次） | 原版成功创建助手 | 候选成功创建助手 | 原版结构通过 | 候选结构通过 |
|---|---:|---:|---:|---:|
| 明确要求先读再委派 | 2/2 | 2/2 | 1/2 | 2/2 |
| 自然复杂任务，不提委派 | 0/2 | 0/2 | 1/2 | 2/2 |
| 简单任务 | 0/2 | 0/2 | 2/2 | 2/2 |

四次成功创建都发生在主方实际读取文件之后，证明真实模型能在执行中调用委派接口。四个助手都实际读过源码，分别有 5、5、4、4 个读取/搜索结果进入成功模型请求；但全部因累计 Token 预算耗尽而失败，完成报告为 0，成功报告交接为 0。两版第一轮主方仅收到 pending 后继续本地工作，第二轮又收到 failed。不能把失败报告的读取当作完成报告的交接。

原 Product 交接诊断只统计 completed 报告之前的证据，失败助手的 `child_visible_source_outputs=[]` 不表示它没读过。新增离线诊断通过原 Session 的成功请求回执查看失败前已见结果，单独标明这不是成功交接，也不替换原评价规则。

**结论：真实“按要求中途派助手”已有证据；自然任务自主选择、有用的完整交接、多 Agent 收益仍未证实。** 这组两次重复的小样不能证明模型永远不会委派，也无法直接观察模型选择本地工作的内部原因。

## 2. 候选改了什么，为何不采用

只通过现有 `evaluation.variants.apply_candidate` 修改 `DELEGATE_GUIDANCE` 和 `COLLECT_GUIDANCE` 两个白名单说明字符串：建议拆出独立小问题、提供已知定位线索、要求短证据交付，并说明 pending/failed/completed 的后续处理。原源码未修改；候选在冻结源码目录、独立进程和原 Product 运行主线中执行。没有改变权限、预算、Verifier、状态机或采用规则。

候选没有改善自然委派或完整交接。它还暴露出自身设计缺口：包含“读取相关范围”的建议，但当前 Product 的 `read_file` 只有 path，整文件返回且没有行号；`search_text` 只给匹配行，没有附近正文。只读子方也没有 shell 或 `read_tool_output`。因此不能把这段说明视为已经可执行的阅读策略，更不能因结构分数从 4/6 到 6/6 就采用。

源码 owner 是 [`ReadFileTool`](../../src/traceh/tools/builtins/read_file.py)、[`SearchTextTool`](../../src/traceh/tools/builtins/search_text.py) 和 [`Product 装配`](../../src/traceh/product/runtime.py)。四个助手在失败前实际使用 79,722、70,108、67,165、65,894 个 Provider tokens；原配置生命周期额度是 60,000。准入估算及保守账本结算不是服务端费用的绝对硬上限，不能拿 capped settlement 充当实际使用量。本轮保留两种量，不修改原预算协议。

整文件反复进入请求是可观察的成本来源，缺少范围读取和行号是已核实的工具能力限制；这些事实尚不能证明只补读取工具就会让模型主动委派。

## 3. 结构检查没有冒充语义成功

冻结 Verifier 只检查：原源码未改、仅新增指定输出、JSON 结构有效、引用文件及行号范围存在。它不判断结论真假。

本助手对六份结构通过的复杂答复逐份核读原 CAS 补丁和对应源码，全部发现关键错误。此为 **Codex 源码审阅，未经用户人工金标确认**，另存[审阅记录](../validation-data/dynamic-collaboration/autonomy-diagnostic/source-review.json)，没有回写原评分：

| 答复 | 关键错误示例 |
|---|---|
| 原版明确委派第 2 次 | 把 grant_root 当作主方收尾额度机制，把 usage_settled 当产物捕获 |
| 原版自然任务第 2 次 | 把预留提交/释放当 retained_tokens，把 report 当产物捕获 |
| 候选明确委派第 1、2 次 | 把 close_account 当收尾额度保留；对历史继承、产物捕获也有错误 |
| 候选自然任务第 1、2 次 | 把 AgentRunReportReader 当作产物捕获；未解释真实 retained_tokens 准入 |

原代码实际是：`BudgetLedgerService.reserve_child` 将 retained_tokens 交给同一 Projector 的容量准入；`Workflow` 等主方整树 dispose 后才调用原 Artifact capture；调查方创建时明确拒绝父历史继承。对应 [`budgets/service.py`](../../src/traceh/budgets/service.py)、[`workflow/execution.py`](../../src/traceh/workflow/execution.py)、[`supervision/delegation.py`](../../src/traceh/supervision/delegation.py)。另两份复杂任务因硬失败不具备通过资格；四份简单任务的版本值通过确定性检查。

## 4. 成本、隔离和重放

两版各 75 次真实 qwen-plus 请求，合计 **150 次真实 API 调用、1,748,884 个精确 Provider tokens**，无 Provider 失败。原版 887,809，候选 861,075；顺序运行、非随机实验，不把约 3% 的差异当作效率收益。请求/重放总数还包括 24 份零用量 scripted requester 请求，合计 **40 Sessions / 174 requests**。

两版都用当前 Product 3、相同已授权连接、直连、90 秒 timeout、不重试，沿原 EvaluationRunner / ProductTaskEvaluator 执行。每个 trial 使用独立 Git 与原显式 Docker 沙箱，network=none，无插件额外授权；任务/主方/子方累计额度沿用 600k/300k/60k，不临时增加。12 个 trial 的 Budget 全部收敛、Workspace live=0。自然任务原版的一次主方预算耗尽与四次助手耗尽是 Runtime 失败，不是连接故障。

独立进程分别用原版与候选冻结源码重放各 20 Sessions / 87 requests，不变量和重建检查通过。第一次重开清理了空 WAL/SHM 临时文件，文件集合检查据实报错；持久数据库/CAS/报告没有变化。Reader 串行收敛后严格重放再次通过，原版 636、候选 653 个文件在最终复核期间逐字节不变。没有新 Provider 调用。

完整派生计数见[汇总](../validation-data/dynamic-collaboration/autonomy-diagnostic/summary.json)。原请求和补丁仍保留在本机原实验目录；源码归档、候选文本和材料摘要均保留。候选准备时的驱动归档发生在原版完成后、候选开始前，不宣称是原版运行前的时间戳证据。

## 5. 交付与下一步

本轮新增显式材料、诊断运行、闭合账本审计、受限候选准备、失败前可见性诊断和对应测试，均位于 `tests/live_dynamic_collaboration/`，不新增生产评估器或事实源。普通 pytest 不调用真实 API。运行后修正了驱动停止条件：必须有原 Directory/Inbox 证明的已接受子任务，仅出现一次被拒绝的 delegate 调用不能通过正向控制。已完成实测中的四次调用都确实创建并接受，因此该修正不改变原结果；实际执行过的旧驱动副本保留。

定向与相邻诊断检查 **16 passed**；恢复“只看调用次数”的旧停止逻辑时，新反例按预期失败，恢复后通过。compileall、修改范围 Ruff、collect-only（3978）、diff 和文档 QA 均执行；未跑全量、L2–L4，未构建新 Wheel，未提交、发行或采用候选。

建议下一阶段先做一项通用能力：**带稳定行号、明确截断/续读信息的有界源码读取**，继续经原 Workspace policy、ToolRuntime、Effect/Session 和请求冻结，不另存业务状态。随后再设计基于原 Budget 投影的调查收尾提示，让助手在额度内交付已证实部分与未知项；不为了取高分强制每题委派。两项都应单独冻结合同并各自做相同条件的真实对照，才能判断责任和收益。本轮没有提前实现它们。
