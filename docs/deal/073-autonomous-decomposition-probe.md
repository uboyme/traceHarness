
# 记录 073：不点名数量时的自主拆分探针

2026-09-14。用户授权在 A–E 与记录 072 之后继续真实调用。本轮问的是一个新问题：**题面不写任何数量时，模型会把工作拆成几个助手？**

## 冻结口径

驱动 `tests/live_dynamic_collaboration/autonomy_probe.py`，材料 `autonomy_materials.py`：五个模块的遥测流水线，其中 `telemetry_parse` / `telemetry_rules` / `telemetry_format` / `telemetry_stats` 在 INDEX.md 里**各自独立成节**，`telemetry_report` 组合前四者。requirement 陈述了"每节都是自足规格，模块间的调用是接口约定而非对未完成工作的依赖"这一事实，并要求"按你的判断分配工作、每个 assignment 给精确路径、两个 assignment 不得碰同一文件"——**全文没有出现任何数量**（契约字段 `assistant_count_named_in_requirement: false`）。

宿主授权 4 个助手（`coder.budget.max_children=4`、进程槽 root 5 / coder 4）。上限：整树 60 次调用、900 秒、连接 60 秒、零重试、一题一次。两轮预检都在固定镜像内证明空实现 exit 1、参考实现 exit 0。连接为已获准的 openai-compatible / deepseek-v4-flash。

需要说清楚的边界：当前 multi 模式**本来就要求分工**（ADR-0071），所以本轮测的是**拆分粒度**，不是"模型会不会选择协作"。

## 两次试次的结果

**第一次**（41 次调用：主 21、子 20；613188 exact tokens；313.9 秒）：一次 `submit_collaboration_plan` **首次提交即通过**，模型自己给出 **4 个 assignment**——`module_parse`/`module_rules`/`module_format`/`module_stats`，各自一个文件、零重叠，把组合模块 `telemetry_report.py` 留给自己。4 份独立 Patch 全部捕获，主方 5 次 `read_child_patch` 读完、4 次 `integrate_child_patch` 全部成功，另做 7 次 apply_patch 与 10 次 shell 自测。随后主方在准备进入固定验证时耗尽自身 token（`BudgetExhaustedError: insufficient max_tokens capacity`，主方自用 504740，coder 上限 700000 中已划走 200000）。未跑到固定检查。

**第二次**（36 次调用：主 16、子 20；416530 exact tokens；290.8 秒）：**只把主方 token 上限从 700000 提到 1600000**，题目/评分/镜像/助手额度/调用与时间上限一律不变。模型再次自主给出 **4 个 assignment**（`parse_author`/`rules_author`/`format_author`/`stats_author`，同样一文件一个、不重叠）；首次提交漏了 `paths` 被 schema 判 failed（可纠正），补上后通过。4 份 Patch、4 次整合全部成功，**这次走到了固定验证**：两次执行均失败，主方消费反馈返工一轮后仍未通过，最终 `CollaborationExecutionStopped: verification_failed`，Product/Workflow failed，无 Review 通过、无 Promotion。

离线用同一冻结检查脚本对产出候选复跑，定位首个失败断言：`telemetry_parse.parse(None)` 抛 `TypeError` 而非规格要求的 `ValueError`——**助手未校验顶层输入**。这已是同一缺陷类第三次出现（记录 044、054、072）。

第二次的收敛：6 个预算账户全部关闭、5 次子预留、工作区 1 释放 5 按失败路径隔离留证、4 份沙箱证据全部 converged、38 份请求快照、invariants passed、convergence converged。助手四人合计 109776 tokens（人均约 27000），主方 306754。

## 结论（分开陈述）

- **拆分粒度：两次独立观察都是 4/4 对齐**。题面无数量提示，模型自主拆出的 assignment 数与任务中独立规定的单元数一致，文件划分零重叠，组合工作留给自己。这是本轮的主要结论。
- **不能推广为"自发协作"**：multi 模式强制分工，本轮不回答"会不会协作"；也只有两次观察，不是比率。
- **机制再次全链通过**：4 份独立身份与预算、4 份不可变 Patch、4 次显式整合、按 assignment 的完成证据、固定验证与人工门禁边界不变。
- **功能未通过**：助手漏验顶层输入，固定检查两次失败，无推广。反馈返工机制被真实触发但未修好。
- **收益仍未测量**：没有同题单助手/串行基线；`await_report` 形态下主子模型调用重叠为 0 秒。四路拆分的代价是主方上下文显著膨胀（第一次 50 万 tokens 用尽后停止）。

按停止规则不再追跑同题。真实 TUI 端到端仍未覆盖。精简证据见[自主拆分证据](../validation-data/dynamic-collaboration/autonomous-decomposition/summary.json)。
