# 记录 072：一次分工两个可写助手的真实验收

2026-09-14。用户明确授权 A–E 实施后的真实调用，并在两次失败后授权继续。题目、评分、镜像、连接与上限全程不变，只修正宿主配置与被暴露的源码缺陷；**没有改题追分**。三次试次全部保留。

## 冻结与上限

驱动 `tests/live_dynamic_collaboration/multi_child_acceptance.py` 复用原 `writable_acceptance` 与 EvaluationRunner/Product 主线；材料 `multi_child_materials.py` 是三模块遥测题：`telemetry_rules.normalize`（助手 A）、`telemetry_format.render`（助手 B）、`telemetry_report.summarize/report`（主方）＋两次整合＋自测。requirement **明确要求两个可写助手、路径不重叠**，因此本轮只测机制与交付，**不能**用来说明模型会自发多开助手。

上限：整树 60 次调用、900 秒、单连接 60 秒、零重试、一题一次。预算逐角色冻结：任务 600000、主方子树 460000、每助手 60000、主方保留 40000；时钟 coder 1320000 ms（= 主方单 Turn 600000 + 2×助手 300000 + 余量）、助手各 300000；进程槽 root 3、coder 2。每轮预检都在固定镜像内证明空实现 exit 1、参考实现 exit 0。连接为已获准的 openai-compatible / deepseek-v4-flash。

## 三次试次

**第一次（5 次调用，36172 tokens，45.0 秒，零助手）**：模型提交了完全合法的两助手计划（第一次把 `main_work` 写成字符串被 schema 判 invalid，自行改正后通过）。派发被 `BudgetExhaustedError` 拒绝。重建账本定位维度：主方当前 Turn 已预留 600000 ms，coder 时钟上限 720000 ms，第一个助手需 300000 ms → 超限。属夹具时钟自相矛盾。

**第二次（4 次调用，1 个助手真实运行，41.2 秒）**：两个助手的预算 grant 都成功（children=1→2），第二个助手在**创建**阶段被拒，随后补偿完整：工作区以 `agent-not-created` 释放、grant 释放、第一个助手被 `_converge` 取消、账户全部关闭、Workflow 干净失败。逐事件重建后定位为 `ProcessSlotAuthority`：每个新 Agent 向**每个祖先**各占一个进程槽，任务 root 的 `max_processes=2` 覆盖不了「主方＋两个助手」。

**第三次（25 次调用：主 17、子 8；297928 exact tokens；254.9 秒；未触 60 次上限）**：机制全链通过——一次计划两个 `patch_author`（`rules-impl` / `format-impl`，路径不重叠），两个助手各自捕获独立 Patch（`telemetry_rules.py`、`telemetry_format.py`），主方分别完整读取并两次 `integrate_child_patch`（均 succeeded），`completion_receipt` 返回**每个 assignment 一条** applied 回执（"2 applied receipt(s) match every accepted writable assignment and this target"）。主方另写自己的模块并用 4 次 shell 自测。

固定功能检查**两次均失败**（exit 1），因此无 Review 通过、无 Promotion、Product/Workflow failed。离线用同一冻结检查脚本对产出候选复跑，定位到首个失败断言：助手 A 的 `normalize` 未校验顶层输入类型，`normalize(None)` 抛 `TypeError` 而非规格要求的 `ValueError`。这与记录 044、054 是同一类模型质量问题，不是机制问题。

收敛：1 个 root 授权 + 3 次子预留、4 个预算账户全部关闭；4 个工作区中 1 个释放、3 个按失败路径 `workspace-dirty` 隔离留证；6 份沙箱证据全部 converged；27 份请求快照留存。原始 SQLite、工作区与运行目录保留在本机忽略目录。

## 本轮暴露并修复的三个宿主缺陷

1. **整批资源预检只查 `max_children`**：额度不足时给的是硬失败而非可纠正拒绝。现按整批累计 tokens/steps/tool_calls/wall 与 `ledger.available()` 比对并点名维度；策略新增 `planned_grant(owner)` 复用既有 `grant_for_child`，未声明 grant 的宿主仍由原准入决定。
2. **`BudgetExhaustedError` 不记录维度**：落盘只有「insufficient capacity」，两轮排查都必须重建账本才能定位。现在消息带维度名。
3. **`product_handoffs.investigations` 变量遮蔽**：读取多 assignment 计划回执时覆盖了正在构建的结果列表，导致运行**结束后**的证据收集崩溃（`KeyError: work_digest`）。已修，并补了离线回归用例与反向验证。

前两项属计划 §3 已要求但我在 B 阶段漏实现；第三项是我 D 阶段编辑引入的新缺陷。三项都有定向用例。

## 分开结论

- **机制通过**：一次分工、两个独立身份助手、两份独立 Patch、两次显式整合、按 assignment 的完成证据、部分失败与整树收敛，全部在真实模型下成立。
- **模型选择**：本轮**不构成**证据——题面点名了助手数量。
- **功能不通过**：助手 A 漏验顶层输入，固定检查两次失败，无推广。
- **收益未测量**：没有同题串行/单助手基线，`await_report` 形态本轮也没有产生主子模型调用重叠（overlap 0 秒），不得推断任何提速。

按停止规则不再追跑同题。真实 TUI 端到端仍未覆盖：该驱动需要真实终端，我的工具侧以重定向启动会让 Textual 写屏死锁（已离线复现，与本轮代码无关），需用户在自己终端执行。精简证据见[多子证据](../validation-data/dynamic-collaboration/multi-child/summary.json)。
