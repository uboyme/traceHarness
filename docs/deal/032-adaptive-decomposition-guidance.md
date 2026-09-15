# 032：DA-6 Adaptive 拆分提示实验

## 结论

DA-6 没有把“自然复杂任务会主动委派”包装成已完成功能。五轮受控提示实验中，qwen-plus 在 15 个 Product trial、121 次真实 Provider 调用里一次也没有调用 `delegate_investigation`。所有候选均未采用，生产 `DELEGATE_GUIDANCE` 已恢复到实验前文本；新增的 Adaptive system section 和任务消息提示也已撤回。

这次完成的是根因定位和可复现实验边界：委派工具与真实主子模型链已经存在，问题不在工具是否暴露、中文是否损坏或子方是否可启动，而在当前模型没有把通用提示稳定转成拆分动作。继续叠提示已经没有证据价值，下一阶段应设计一个 typed 的拆分决策步骤，而不是继续写更长 prompt。

## 实验设计

`tests/live_dynamic_collaboration/decomposition.py` 在连接 Provider 前冻结三类材料：两个独立只读主题、一个明确紧耦合状态链、一个简单版本读取。每题的空产物必须在固定、断网 Docker 镜像中失败，参考产物必须通过。真实运行只用原 EvaluationRunner/ProductTaskEvaluator、Product、Workflow、Supervisor、Git、SQLite、Budget 与 Sandbox；不跑基线、语义裁判、旧 72 题、全量或 L2。

每轮一题一次，不重试模型。正例要求 Product 任务通过、真实 delegate、真实 child call、collect 和报告回主方；两个负例要求 Product 任务通过且不创建助手。题目没有写委派工具名，也没有命令模型必须派助手。

## 候选与结果

| 轮次 | 唯一策略变化 | 可拆题 | 紧耦合题 | 简单题 | 委派 |
|---|---|---:|---:|---:|---:|
| 1 | 工具说明加入“先判断、可选拆分” | 通过 | 调用上限 | 通过 | 0 |
| 2 | 工具说明改成满足条件时执行 | 通过 | 通过 | 通过 | 0 |
| 3 | 同一说明同时进入 Adaptive system section | 通过 | 通过 | 通过 | 0 |
| 4 | 多主题默认可拆，目录发现后下一动作必须委派 | 通过 | 通过 | 通过 | 0 |
| 5 | 任务消息再显式提示本次为 Adaptive | 未通过 | 调用上限 | 通过 | 0 |

共 15 个 Product trial、121 次真实 qwen-plus 调用；全部是主方调用，child call、delegate 和 collect 均为 0。成功模型记录还包括 30 次零用量本地 requester，共 151 次成功、940,445 exact tokens；2 次本地调用上限拒绝没有连接 Provider。所有任务 Budget 收敛、Workspace live=0。

请求快照证明第 3～5 轮的主方确实看到了 Adaptive system section 和委派工具。模型还准确用英文复述了中文题目的 allocation/handoff 两个主题。进一步按文件字节、Python Unicode 对象和事件日志三条路径核查，替换字符计数为 0；终端显示的乱码只是 PowerShell 输出渲染，所以没有为此修改生产编码。

## 最强提示原文

最后测试的拆分规则是：

> In Adaptive mode, after minimal directory or index discovery and before reading topic source files, classify the task once. When the requirement lists two or more distinct investigation topics or source groups, treat them as independent unless one explicitly depends on another's result or on uncommitted changes. In that separable case, your next action must be delegate_investigation for one bounded topic; investigate a different topic yourself while the child runs. The delegated topic must be answerable from the original source revision alone and request a concrete evidence deliverable. Keep a simple task or an explicitly tightly coupled reasoning chain local. Do not forward the whole task. Give a specific goal, useful briefing and the evidence expected. Do not duplicate the child's assigned source reading unless checking a specific claim after collection. Collect the exact returned message before using its findings.

第五轮还在 Workflow 任务消息中增加了：

> Execution strategy: Adaptive. Before implementing the requirement, apply the traceh.product.adaptive-decomposition system section.

这两段均已撤回。当前生产说明恢复为实验前的五句短文本，因此仓库里没有保留一个已经被真实证据否定的隐藏默认策略。

## 下一层归属

下一步不应让 Agent 自由输出一段计划后由宿主猜。比较合适的是 Adaptive 在普通执行前产生 typed 决策：`local` 或 `separable`；`separable` 必须给出有界子目标、原来源身份、证据交付和主方保留工作。程序验证两份工作没有显式依赖后才调用现有 delegate；验证失败回到 local。这个步骤只决定执行形状，不拥有 Product 状态、任务事实、Workspace、Budget 或 Promotion，也不能让模型绕过现有人工批准。

这属于后续新合同，本轮没有提前实现。

## 最终恢复门禁

撤回候选后，DA-6 合同、Registry、Adaptive Product、调查工具和 Product Assembly 共 31 项定向测试通过，其中 Adaptive Product 显式使用现有固定 Docker 镜像。`compileall`、修改范围 Ruff、全仓 4025 项 collect-only、反示例硬编码扫描、文档链接/章节/围栏/JSON 检查和 `git diff --check` 均通过。没有运行全量 pytest 或 L2，也没有提交或发布。
