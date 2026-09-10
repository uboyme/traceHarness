# ADR-0066：共享 Evaluation 合同与受限优化控制面

日期：2026-09-10。状态：设计采用；UE-0～UE-3 已授权接线，UE-4/AO 仍待后续。当前实施见 [UE-3 合同](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_CONTRACT.md)，以下设计决定不变。
依据当前 v0.10.0 源码；不会使本文描述的拟议接口自动成为现有能力。

## 问题

现有 `traceh eval` 的 manifest、runner 和报告直接面向 ProductTask；主动检索的 72 条旅程另有测试目录实验运行器。
未来优化需要共同的冻结身份、预算、证据和比较，但 Product 成功与检索成功不能用一个布尔字段代替。
现有 L3/L4 面向确切插件 Wheel，不能解释为任意核心提示改动的评测/采用权限。

## 决定

1. 保留唯一 `traceh eval` 入口，在 `evaluation` 内抽取共同输入、运行、结果和比较合同；
   首版只有 ProductTaskEvaluator 与 RetrievalEpisodeEvaluator，一份 manifest 声明一种任务类型。
2. evaluator 是宿主静态装配的可信组件，复用原生产入口与 Projection/Reader；不开放任意模块导入，
   不增加 AgentLoop、业务状态机或模型可调用的评分工具。
3. 公共结果分开 execution、assessment、invariants、convergence、usage 和 evidence。
   运行测量完整不等于业务成功；评分未审、证据缺失和未知用量不能按通过或零处理。
4. 实验输入与人工 judgment 是宿主不可变评估工件；Session、Effect、Budget、Product 等事实仍在原 EventStore，
   原文/产物在原 CAS，ref 在 Git。报告为派生视图；不建立另一个运行状态账本。
5. baseline/candidate 是变体；single/multi/auto 是 Product 执行模式。比较冻结共同条件与允许差异，
   不要求优化前后 prompt 字节相同；具体实际请求分别留证据。
6. 计划升级 benchmark 根协议至 3；切换时同时改当前调用方和 shipped 数据，旧协议明确拒绝，
   历史工件由对应版本代码审计。不保留第二 runner、兼容别名或自动数据迁移。
7. 72 条旧旅程作为开发/回归集，迁入正式 evaluator；Product 内现有 F5 检索度量保留为该类型组件。
   不把单来源隔离实验描述成多来源开放检索能力；不把旧 55/72 作为新一轮实测成绩。
8. 通用候选比较属于 `evaluation`；多轮实验、去重、停止属于 `evolution`。
   后续优化插件经原 typed provide/require 提供策略服务，只提出受限候选，不定义评分、不改预算/权限、不给自己批准。
9. 初版核心文本候选使用冻结源码 patch、独立进程和原沙箱生产入口，限可信本地代码。
   原 L2/L3/L4 的确切 Wheel 门禁和 Registry 不变，不强制把核心 patch 包装成插件，也不绕过 Wheel 门禁。
10. 保留人工语义评分点；没有稳定收益可停止并保留基线。优化报告不自动 commit、安装、推广或发布。

## 取舍与边界

共享的是实验治理与证据合同，不是业务成功定义。两种真实任务足以验证抽象，暂不实现未来 MCP/Workflow/Safety 的空接口。
保持独立运行工件与业务事实的所有权区别，避免为实验追踪复制第二份 Session 状态。
可信 Python 插件接口约束不等于对恶意插件的 OS 隔离；没有全局跨 Store token 账本、冷恢复或可靠通用模型裁判的承诺。

不改写 ADR-0033 的历史决定；本 ADR 在其唯一 eval 入口和生产证据原则上扩展任务种类。
具体字段、文件改动、阶段和验证见[执行设计](../plan/TRACEHARNESS_UNIFIED_EVALUATION_AND_OPTIMIZATION_DESIGN.md)。

实施合同与当前范围见 [UE-0/UE-1](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE0_CONTRACT.md)；历史决定不因文件迁移而改写。
