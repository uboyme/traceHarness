# DA 真实实例：材料、模型和调用边界

本合同在 DA 真实 Provider 请求之前冻结。工程基线为 AO-3 已发行后当前 DA 工作代码；每批实际源码摘要、材料、计划、连接身份与沙箱摘要由原 Evaluation 冻结并写入 `batch-contract.json` / `experiment.json`。不能用发行版本字符串替代源码身份。

## 1. 材料与分组

独立目录 `benchmarks/dynamic_collaboration_v1/development` 为 12 题，holdout 为 6 题。三类数量分别为 4/4/4 和 2/2/2。题目由真实仓库合同缩减重建，不声称逐字复用了生产代码、历史真实会话或独立抽样。每题一个问题组；留出无相同问题换名、换数值的开发副本。全部标注来源文件内容摘要。

材料生成后用固定本地 Python Docker 验证：故障版在合同断言处失败，参考修复通过。导入、容器启动或语法失败不能替代负例证明。检查脚本及参考修复不进入 Agent checkout。所有题有独立确定性 VerificationPlan 和 Product 语义 rubric；隐藏检查不是模型可修改的代码。

## 2. 调用配置

- Provider：现有用户授权的 OpenAI-compatible 连接，模型 `qwen-plus`；此为实例值，不写入通用默认。
- 网络：直连，任务和裁判无自动重试；单请求 Provider timeout 90 秒。网络失败保留原分类及未知 usage，不补跑直到成功。
- 工具/验证：已配置 Docker `desktop-linux`，固定镜像 `sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b`，network=none，无插件额外授权。公共沙箱文件在每批调用前复制冻结，沿原 Sandbox owner 执行。
- 凭据：私下复用既有配置加载器，既不打印也不进入计划/报告。实际端点以冻结的公开连接身份和摘要核对。
- 源码：独立安装本阶段 Wheel，原 paired workers 再按相同源码冻结各臂；模型请求最终由原 Session/Effect 重放证明。进程隔离与工具容器隔离分别报告。

## 3. 明确额度

| 范围 | Token 包络 | steps / tools | 累计 wall | 直接子数 / 剩余深度 / 子并发槽 |
|---|---:|---:|---:|---:|
| 任务根 | 600,000 | 120 / 160 | 1,800,000 ms | 4 / 2 / 3 |
| 主 Agent | 300,000（含调查预留） | 60 / 80 | 900,000 ms | 3 / 1 / 2 |
| 每个调查 | 60,000 | 16 / 20 | 180,000 ms | 0 / 0 / 0 |

主方单 Turn 上限 300,000 ms，调查 120,000 ms；输出上限分别 8,192 / 4,096 Token；主方每次委派后的保留量 32,000 Token。父预留不与子消费重复相加。未知模型使用量与保守预算收费分开。

每个独立两臂实验 timeout 14,400 秒、worker shutdown 120 秒；先后重复属于不同冻结实验。语义裁判每个成功可审任务最多一次，单次输入/输出包络 64,000 Token，输出 2,048、安全预留 2,048，90 秒；调用前冻结 rubric 和预算。一次优化提案最多 32,000 Token，输出 6,000、安全预留 1,024，90 秒；最多一份候选，不自动续轮。

任务 trial 基础最多 68，条件性追加最多 48，总计最多 116；任务根总包络最多 69,600,000 Token，裁判最多 116 次 / 7,424,000 Token，提案最多 1 次 / 32,000 Token。合计是保守的额度上限，不是预计或实际消耗；实际 Session usage 独立报告。材料验证没有调用模型。

## 4. 顺序与停止

1. A/A：current-question-order、worker-receipt-identity；single/single，两次独立重复，第一轮 baseline 先跑，第二轮 candidate 先跑，8 个 trial。
2. 机制：bounded-identity、current-question-order、memory-scope-and-state、worker-receipt-identity、cancel-before-worker-start、unknown-usage-qualification；single/adaptive 各一次，共 12 个 trial。
3. 开发：全部 12 题，single/adaptive 各两次，独立 AB/BA 顺序，共 48 个 trial。
4. 受限优化：只从开发证据提出最多一份四个委派说明常量范围内的候选。原 AO 比较六道机制开发题，adaptive/adaptive、各两次，共最多 24 个 trial；原同候选两臂先后执行，不声称已消除所有顺序效应。无候选不启动该批。
5. 条件性留出：基础 adaptive 或可审候选达到开发资格后，才冻结最终实验源码做 6 题 × 2 策略 × 2 次，共 24 个 trial。留出不得流入优化提案。未满足资格就停止，不用留出继续调参。

硬身份、权限、取消收敛或持久证据失效时先封存批次并修复；修复前后不能拼成同一对照。网络故障、模型误答、预算停止如实保留；不擅自扩大轮次。工程上可默认保留 single，让 adaptive 显式可选，不以“必须有提升”为阶段完成条件。

## 5. 判断与解释

原 Product durable gate + 原语义 assessment 联合解释任务质量。调用次数、整树实际 Token、等待/墙钟、失败/取消成本、交接消费分列；模型待审、未知 usage 不判为赢。晋级要求硬门禁、质量无确认退步，并且至少一个组在两次重复中改善，或同质量且 wall elapsed 至少减少 10%；整树 Token 最多 single 的 1.50 倍。通用 comparison 给出配对变化与硬指标，实例资格另外按预先声明的分组和重复解释，不改评分器。

样本小、题目是合同缩减重建、裁判来自模型，因此只作描述性证据，不作总体显著性或跨模型泛化结论。最后由独立读取进程重新打开原 SQLite/CAS/请求，核对期间原文件未变。用户真实工作区不参与代码重放，候选不自动推广。
