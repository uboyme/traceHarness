# WC-1C：真实任务完成，但未触发助手

2026-09-12。结论：**本次协作验收未通过，不进入 WC-2**。

## 实际结果

| 检查项 | 结果 |
|---|---|
| 真实模型 | qwen-plus，直连，无重试 |
| 调用 | 主方 7 次，助手 0 次；未到 16 次上限 |
| Token | 22107 input + 1679 output = 23786，Provider exact usage |
| 任务耗时 | 73.375 秒；Provider active 40.965 秒 |
| 结构化决定 | 一次合法 local，没有 delegate |
| Product / Workflow | completed / completed |
| 固定验证 | 11 个函数用例及源文件边界通过 |
| 助手、报告、使用报告 | 未发生，不能算通过 |
| 网络失败 | 0 |
| 预算 / Workspace | 2/2 账户关闭、所有预留终结，0 live / 0 quarantine |
| 重放 | 3 个 Session、9 份请求，零违规，原数据库摘要不变 |

9 份请求包含 7 次真实主模型请求和 2 份 Evaluation 控制流程的脚本请求，不能称为 9 次
真实模型调用。预算中的 child reservation 包括 Workflow 对主执行 Agent 的分配，不能误当作
调查助手已经创建。原 EvaluationRunner 在隔离 benchmark 内完成批准和 Promotion；没有操作
用户项目远程或发布本仓库。

## 测试过程与证据

固定任务是实现 `recommend(stock, target, pack, blocked=False)`，补货规则在 `rules.md`。
任务明确要求只读助手检查规则并给文件行证据，主方据此编码。正确实现没有交给模型。
先用真实 Docker 确认占位实现失败、参考实现通过，再冻结源码、驱动、材料和连接身份。

真实主方依次进行了目录查看、规则读取、local 决定、目标模块读取、代码修改、shell 检查和
最终回答。原宿主随后执行固定验证并完成 Product。主方代码正确处理禁售、库存足够、整包
取整和非法边界，但没有履行测试要求的协作过程。

模型给出的决定理由是：规则正文已经通过 read_file 获得，证据完整，不需要再委派。
复核冻结 decision 输入，原始“请明确委派一个只读助手”要求确实存在，没有丢失。
当前系统说明又允许 “Simple work can stay local”，模型选择依据后者自行完成。

因此，本次可定位到**协作意图与是否委派的决策边界**：程序保证合法 delegate 会派发并收报告，
但尚未让“用户明确要求协作”成为必须满足的执行约束。此例未走派发分支，不能诊断为助手
运行失败、网络失败，也不能用 Product completed 冒充协作验收成功。独立决定视图将任务装在
来源数据中这一呈现是否影响优先级，需要另立受控实验，不能由一次结果断言唯一原因。

本题材料较小，主方在侦察中已读完全部规则，客观上降低了委派的实际价值。它暴露了明确协作
要求未落实，但不能推广为“所有复杂任务都无法委派”，也没有证明扩大任务就一定能解决。

按[冻结合同](../plan/TRACEHARNESS_WC1C_REAL_ACCEPTANCE.md)停止：未修改提示、材料或预算重跑，
未调用额外裁判或基线。WC-1B 候选保留在未提交工作区，未推广为已验收默认策略；未恢复平行
旧派发链。下一步应先审查“明确要求委派”与“允许模型自行选 local”的区别，再决定新合同。

## 必要仪表修正

Evaluation 报告可见性 Reader 原来只识别 collect_investigation。运行前补充识别
decide_task_collaboration 的 child 报告，仍逐一匹配原助手、消息、输入摘要、revision 和
statement，不更改任务评分或仅凭存在工具调用判成功。

真实 Product/Supervisor 的确定性委派用例确认报告进入指标；在临时源码副本中移除识别后，
该用例因 parent_report_dispatches 为空而失败，证明确实检查到了目标行为。原源码保持正确。
驱动还验证了调用封顶、调用失败/取消后不再调用，以及材料篡改拒绝。

WC-1C 定向验证累计 65 项不同用例通过，另有 1 项报告可见性反向检查有效；compileall、4104 项仅收集、7 个本轮 Python 文件 Ruff 通过。旧测试夹具已适配当前侦察/决定阶段，真实实验未重跑；示例名称仅用于测试材料，未写入通用生产规则。

没有全量测试、L2–L4、联网安装或 Wheel；没有提交、推送、tag、release。

## 可查阅材料

- [验收摘要](../validation-data/dynamic-collaboration/wc1c-20260912/summary.json)
- [原评估报告](../validation-data/dynamic-collaboration/wc1c-20260912/evaluation-report.json)
- [固定题目及验证器](../validation-data/dynamic-collaboration/wc1c-20260912/dataset.json)
- [实际实现](../validation-data/dynamic-collaboration/wc1c-20260912/actual-implementation.txt)
- [重放核对](../validation-data/dynamic-collaboration/wc1c-20260912/replay.json)

原完整日志、CAS、隔离 Git 目标与运行材料保留在 `.traceh/wc1c-real-20260912/`。
导出的文档材料是审阅副本，不是新的事实源或可直接重启的实验目录。
