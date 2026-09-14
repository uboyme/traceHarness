# 记录 071：按需并发交接的真实验收

2026-09-13。用户在实现并发机制后明确授权一次真实调用验证。本轮一题一次、整树最多 32 次调用、600 秒、连接 60 秒、零重试，材料/源码/沙箱运行前冻结，停止规则为"模型选串行或固定检查失败就停止并如实报告"。

## 冻结与预检

驱动 `tests/live_dynamic_collaboration/concurrent_acceptance.py` 复用原 `writable_acceptance`（原 EvaluationRunner / ProductTaskEvaluator / Product 主线），沿用 WC-4 遥测夹具的 FILES、REFERENCE 与 CHECKS 字节，只替换 requirement：新增一句事实说明——INDEX.md 完整规定了两个模块，`telemetry_report.summarize` 可仅依据规格编写，不需要读助手代码。requirement **没有出现 handoff、并发或任何模式名称**，契约里以 `handoff_mode_named_in_requirement: false` 固定该事实。夹具预算沿用记录 067 的显式测试额度（task 480000 / coder 360000），不改生产默认值。

预检在既有固定镜像（`python:3.12.13-slim-bookworm`，digest 固定，`--pull never`、network none）中证明空实现退出码 1 且带 AssertionError、参考实现退出码 0。未启动或修复 Docker 环境，未拉取镜像。

连接复用已获准的 openai-compatible / deepseek-v4-flash，密钥只从环境读取，未打印；连接摘要以 base_url 指纹记录。

## 实际结果

15 次真实调用（主 11、子 4），115656 exact tokens（输入 108772 / 输出 6884），unattributed 0，114.453 秒，无 Provider 失败或重试，未触及 32 次上限。

主方在唯一一次 `submit_collaboration_plan` 中**自行选择 `dispatch_and_continue`**，工具结果为 succeeded。随后原日志的时间线（UTC）显示真实交错：

- 12:28:44.448 主方提交分工；12:28:46.502 助手 Turn 开始，12:28:46.725 助手首个模型请求；
- 12:28:46.847 主方下一次模型请求（助手运行中，主方没有等待）；
- 12:28:48.685/48.714 助手读源码；**12:28:52.801 主方 apply_patch 写自己的 telemetry_report.py**；
- 12:28:54.968 助手 apply_patch 写 telemetry_rules.py；12:28:56.793 主方调用 `collect_child_patch`；
- 助手 Turn 于 12:29:05.326 结束，主方在 12:29:08.282 继续，随后 `read_child_patch` → `integrate_child_patch` → 自测 shell → 收尾。

两条独立并发度量：驱动记录的主/子 Provider 在飞区间重叠 6.905 秒；原账本派生的整树累计工作时长 121534 ms 比墙钟 114453 ms 多 7081 ms。两者一致，但只证明模型调用时间重叠，不是 CPU 并行，也不构成提速结论。

工具计数：`submit_collaboration_plan` 1、`collect_child_patch` 1（一次即完成，未出现 pending）、`read_child_patch` 1、`integrate_child_patch` 1、`apply_patch` 2（主子各一）、`read_file` 8、`list_files` 1、`shell` 1。

## 交付与收敛

Product/Workflow completed；固定功能检查一次通过（`verification/result.passed = true`）；Review 通过；隔离基准目标推广提交，`patch/review-recorded`、`patch/approval-recorded`、`patch/promotion-committed` 齐全且新修订即目标 ref 实际指向。assessment passed、invariants passed、convergence converged、errors 为空。

两份 Artifact：助手 `telemetry_rules.py`，主方汇总 `telemetry_report.py` + `telemetry_rules.py`。3 个预算账户全部关闭，17 笔使用预留与 2 笔子预留终结，结算 115656 tokens；3 个工作区全部释放、live 0、quarantined 0；3 份沙箱证据收敛。

独立离线核验：把原 SQLite 复制到临时目录后重放 4 个 Session 的 17 份 request 快照，全部通过，原库 SHA-256 前后一致（`5803759a…`）。

## 结论与边界

分开陈述：**机制通过**（并发派发、主方并行工作、按需收集并捕获真实 Patch、显式整合、完成证据取自收集回执）；**模型选择成立**（在题目声明两模块独立时自行选了并发，题目未提示模式名）；**功能通过**（固定检查一次通过）；**收益未测量**——本轮没有同 requirement 的串行基线，7 秒重叠只是观察量，不能换算成提速或质量结论。

一题一次不能推断普遍可靠性；推广只发生在隔离基准目标，不是项目仓库发版。原始运行目录、工作区与 SQLite 保留在本机忽略目录，精简证据见[并发交接证据](../validation-data/dynamic-collaboration/concurrent-handoff/README.md)。本轮未追加调用、未改题重跑、未提高上限。
