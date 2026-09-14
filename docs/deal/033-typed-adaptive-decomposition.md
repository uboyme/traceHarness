# 033：DA-7 类型化 Adaptive 拆分决策实验

DA-7 把 DA-6 的提示词假设推进成一个可执行候选：显式 `adaptive` 主 Agent 在普通工作前必须调用 `decide_task_decomposition`，以结构化字段选择 `local` 或 `separable`。候选通过了确定性合同测试，但四轮真实 `qwen-plus` 小样没有一次选择 `separable` 或创建助手。按照事先冻结的停止规则，生产接入已撤回；当前 `single` 与原 `adaptive` 运行方式没有变化。

## 候选怎样接入原架构

- 决策只写原 Session 的 Tool call/result 与 Effect；没有 Product 事件、新表、可变 `messages` 或第二事实源。
- `separable` 复用现有只读调查 owner，Budget、Workspace、Supervisor、Inbox、Delivery 和报告收回仍由原模块负责。
- 决策前 Policy 拒绝普通读写、shell 和直接委派；缺失决定最多给两次固定修复，随后 fail closed。
- `local` 禁止创建调查；`separable` 创建的确切子身份必须在主方结束前收到 terminal 报告。
- 默认 `single` 不加载该合同；决策不能增加权限、预算、深度，不能批准、推广、提交或发布。

确定性 Provider 覆盖了合法 local/separable、重复决定、漏答、非法依赖、错误 Session/owner、pending/terminal 收回、失败与取消。它证明程序可以验证并执行这份协议，却不能证明真实模型会选择正确路径。

## 真实实验怎样进行

每轮固定同一 Provider/模型、直连、每请求一次尝试、固定断网 Docker Verifier，并各跑一条可拆双目标、一条紧耦合链和一条简单读取。每轮都保留 Session、Effect、请求、Budget、Workspace 和报告；不跑 baseline、语义裁判或旧 72 题。

| 指标 | 结果 |
|---|---:|
| 轮次 / 场景 trial | 4 / 12 |
| 真实 Provider 调用 | 68 |
| 预期 `separable` trial | 4 |
| 实际 `separable` / 助手创建 | 0 / 0 |
| 与冻结期望一致的 typed 选择 | 5 |
| Product 任务成功 / 完整行为通过 | 3 / 2 |
| Budget 收敛 / Workspace 泄漏 | 12/12 / 0 |

第一轮发现 `local` schema 错误地要求 `main_goal` 也必须为空，这是候选合同自己的缺陷。修正后，模型能够在部分紧耦合和简单题选择 `local`。随后只做了通用修正：说明按独立可回答目标计数、明确“自己能做完”不是选择 local 的理由、把决策 Tool 排到前面，并把漏答修复限制为两次。最终仍有两种稳定失败：模型在必经检查点前调用普通读取工具；或者面对确实可拆的任务，因为自己也能完成而选择 `local`。

因此根因不再是“没有委派工具”或“typed schema 完全不可用”，而是当前 Tool UX 同时展示必经决策与普通工具时，真实模型没有稳定遵守第一检查点；即使进入检查点，结构化字段也不足以让它改变执行偏好。继续加提示、改题或放宽验证没有证据价值。

## 决定与后续边界

冻结合同明确要求分类仍不可靠时撤回生产接入。候选 Tool、Policy、Reader 和 Evaluation 字段已经删除，原 Adaptive 委派工具和默认 single 得到定向回归确认。实验数据见[验证目录](../validation-data/dynamic-collaboration/typed-decomposition/README.md)。

撤回后复跑 Adaptive、Evaluation、Registry、架构保护与 Product Git/Docker 相邻主线，共 47 项通过。`compileall`、修改范围 Ruff、4025 项 collect-only、JSON/文档链接/围栏及 `git diff --check` 均通过。没有运行全量、L2–L4、Wheel，也没有提交或发布。

下一步若继续，应先冻结一个新的设计合同，研究在普通工具可见前提供**独占的决策界面**或独立 planner checkpoint。它仍应把事实写入原 Session，复用原 Budget/Supervisor，不拥有 Product/Workflow/Workspace/Promotion，也不能在 `AgentLoop` 里临时加一个不可审计分支。这个方向目前只是待验证假设，DA-7 没有实现或证明它。
