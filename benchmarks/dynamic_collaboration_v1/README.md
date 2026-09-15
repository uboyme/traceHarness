# DA：按需协作的任务材料

这些是依据 TraceHarness 当前真实合同**缩减重建**的可运行小项目，不是生产仓库的原始缺陷快照，也不是随机抽样的通用能力榜单。每题的 `provenance.json` 条目标明依据文件和当时内容摘要；生成器保留在 `tests/live_dynamic_collaboration`。没有用模型实验结果筛选或删除题目。

`development` 有 12 个不同问题组，`holdout` 有 6 个不同问题组；各自是独立 Product benchmark 目录。优化模板只绑定 development。每个工作区只接收该题的初始文件、需求和 `.gitignore`，不接收隐藏检查、参考修复或其他题目。用户需求不要求委派或指定角色。

这批是合同缩减题：已运行的 multi 材料实际只有 2～16 行 Python，不能单独证明模型能在复杂仓库中自主分工。后续 [12 个实际源码诊断任务](../../docs/deal/028-autonomous-delegation-diagnosis.md)另行冻结和计数，不替换这里的原数据或评分；仍未证明自主完整交接。

| 类别 | 开发题 | 留出题 |
|---|---|---|
| 局部修复 | 身份边界、嵌套事件隔离、Unicode 分页、当前问题编排 | 严格整数额度、真实路径包含关系 |
| 可独立核对多个模块 | Memory 状态与项目范围、worker 回执配对、工具全文保留、Profile 实际装配 | 验证与补丁绑定、Skill 版本与资源引用 |
| 依赖状态或缺少确定信息 | 启动前取消、提交后异常补偿、过期来源计划、未知 usage 不晋级 | 原子消息领取、冻结请求重试 |

每题单独声明 VerificationPlan，成功仍由原 ProductTaskEvaluator 判定。所有题声明 `product-durable-semantic-v1`，程序通过后还需原 review/assess 中的语义审阅；模型意见不冒充人工标准答案，不能覆盖硬失败。

`material-validation.json` 记录真实本地 Docker、固定镜像、禁用网络下的 36 次材料检查：每题故障版本必须到达合同断言并失败，参考修复必须通过。它只证明材料有可复现的问题和一个可行修复，**不是 Agent 完成了任务**。真实 Agent 对照结果另行保存。

实例预算是显式实验输入，不是系统默认：任务根 600,000 Token，主方账户 300,000 Token（包含调查预留），调查单份 60,000 Token，委派后给主方保留 32,000 Token；主方最多创建 3 个调查，同时最多 2 个。整树累计与并发仍由原 Budget 约束。single/multi 共用同一套根额度和基础工具。

运行规模、先后顺序、语义裁判与费用上限见 [DA-0 真实实例合同](../../docs/plan/TRACEHARNESS_DYNAMIC_COLLABORATION_DA_REAL_CONTRACT.md)。使用全新输出目录；旧结果不能覆盖、删掉失败或拼成新的同源实验。
