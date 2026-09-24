# 087：任务类型 Single/Multi、Multi 上下文治理与有界自进化（收口计划执行记录）

日期：2026-09-23。计划：[收口计划](../plan/TRACEHARNESS_TASK_TYPE_CONTEXT_EVOLUTION_PLAN.md)。
原始证据：`C:/Users/caojie/tta2`（A）、`C:/Users/caojie/ttb-gov`、`C:/Users/caojie/ttb-ungov`（B）、`C:/Users/caojie/ttc-*`（C），
账本 `.traceh/tta2-ledger.jsonl` 等；预登记与汇总在 [validation-data](../validation-data/task-type-evolution-v1/)。
模型 `qwen3.6-plus`（DashScope OpenAI 兼容端点），所有调用直连，每请求一次 Attempt。
样本很小：每类一题、每臂两次，只作按类观察，不作显著性或普遍优劣结论。

## 1. 付费前的问题与修正

| 事件 | 根因 | 处理 |
|---|---|---|
| 首次启动两份计划在任何模型调用前失败（0 token） | 输出根目录过深，Git for Windows worktree 路径超长 | 输出改到短根 `C:/Users/caojie/tta*` 与 `p01..p10`；证据保留在 `.traceh/tta-runs` |
| tta1：Multi 3 臂中 2 臂 `provider-tool-arguments-json-value-expected`，批次按规则停止 | 重放冻结请求证实：嵌套对象参数 `main_work` 被写成空值/缺失/模板泄漏（7/8），拆为三个顶层字符串后 0/8（[诊断](../validation-data/task-type-evolution-v1/tool-arguments-diagnosis.json)）；最初“字母排序”假设被对照否定 | [ADR-0083](../adr/0083-flat-collaboration-plan-main-work.md) 拆平计划参数；A 在新源码摘要下从头重跑，tta1 结果不合并 |

## 2. 实验 A：五类任务 Single/Multi（tta2，源码摘要 `63b8c927…`）

预登记见 [preregistration-a](../validation-data/task-type-evolution-v1/preregistration-a.json)，逐臂数据见
[a-arms](../validation-data/task-type-evolution-v1/a-arms.json)。Multi 统一授权最多 3 个助手、可写助手可用。

| 类别 | Single 通过 | Multi 通过 | Single token | Multi token | Single 墙钟 s | Multi 墙钟 s |
|---|---:|---:|---|---|---|---|
| A1 小任务 | 2/2 | 2/2 | 89k / 57k | 218k / 203k | 130 / 79 | 236 / 209 |
| A2 并行读码 | 1/2 | 2/2 | 165k / 69k | 593k / 580k | 214 / 103 | 471 / 496 |
| A3 并行改码 | 2/2 | 2/2 | 98k / 123k | 614k / 466k | 110 / 152 | 482 / 372 |
| A4 先调查再修复 | 0/2 | 0/2 | 210k / 194k | 666k / 688k | 221 / 194 | 537 / 568 |
| A5 强依赖 | 0/1 | 0/2 | 187k / 缺臂 | 678k / 未知 | 195 / — | 545 / 615 |

所有已测用量均为 exact；A5 r2 Multi 主方一次在飞调用在 Turn 期限被取消（用量未知），按 S0-C 该配对 Single 臂未启动，
记为缺臂，不补跑。

与事前预期对照：

- A1（预期 Single 成本更低、质量相同）：**符合**；Multi token 约 2.9×、墙钟约 2×。
- A2（预期 Multi 可能更快、token 接近）：**不符合**；Multi 两次都通过而 Single 一次结论有错，但 token 约 5×、墙钟约 3×。
- A3（预期 Multi 可能更快、token 更高）：token 更高**符合**（约 4.9×），更快**不符合**（墙钟约 3.3×）。
- A4（预期不确定）：两模式 0/2。Single 能交付但固定验收失败；Multi 主方反复 `max_steps_exceeded` 与
  `BudgetExhaustedError`，未到验收。
- A5（预期 Single 更好）：两模式均未通过，不能区分。

Multi 在 A4/A5 的失败**混入宿主缺陷**，不能单独归因于模式：分工条目字段集错误的拒绝只写
`collaboration-assignment-fields-invalid`，可写助手必填的 `paths` 无法由共享 schema 表达，一个臂连续 5 次漏交 `paths`
耗尽步数；此外几乎每个 Multi 臂的首份计划都因“助手墙钟 300s 大于单次调用可等 55s”被拒一次（ADR-0079 既定合同）。
字段拒绝已改为写明角色、缺少与不允许的字段，定向测试与反向验证通过（正式版 14.3.26 补充）。

上下文：所有 Multi 臂任一 Agent 的单请求输入峰值最高 34,529，低于 96k 窗口 60% 触发线 51,456；折叠 0 次。
Single 峰值最高 18,199。

### 2.1 A4/A5 在宿主缺陷修复后的重跑（`tta45`，源码摘要 `bbe085dd…`）

预登记见 [preregistration-a-rerun](../validation-data/task-type-evolution-v1/preregistration-a-rerun.json)，
数据见 [a45-rerun-arms](../validation-data/task-type-evolution-v1/a45-rerun-arms.json) 与补跑
[a45-redo-arms](../validation-data/task-type-evolution-v1/a45-redo-arms.json)。
计划、模板、模型、预算与 tta2 逐字节相同，只有源码不同：ADR-0083、字段拒绝、ADR-0084、ADR-0085；
后两者只影响折叠与后台检测，96k 下 A 从未折叠。tta2 的行原样保留，不被替换。

| 配对 | Single | Multi | Single token | Multi token | 说明 |
|---|---|---|---|---|---|
| A4 r1 | **通过** | 失败 | 273k | 655k | Multi 首份交付被验收退回，修复中 `BudgetExhaustedError` |
| A4 r2 | **通过**（补跑） | 失败（补跑） | 312k | 684k | Multi 协作被停止（`CollaborationExecutionStopped`）；首次尝试 Multi 超时、Single 未启动 |
| A5 r1 | 通过 / 失败（补跑） | 超时 / 超时（补跑） | 325k / 172k | 未知 | 两次 Multi 都在生成中遇到 120s 超时（见 §5） |
| A5 r2 | 未启动 | 失败（补跑，用量未知） | — | 未知 | Multi 主方在飞调用于 Turn 期限被取消，Single 按臂间停止未启动 |

- **A4（先调查再修改）有两组有效配对，都是 Single 通过、Multi 失败，Multi token 为 Single 的 2.2–2.4 倍。**
  宿主缺陷修复后仍如此，这是本题上真实的模式结论：Single 更好。
- A5 在 120s 超时下没有有效配对。按预登记修正，以 300s 请求超时重跑两组（`tta5t`，源码 `9dcaf759…`，
  计划除超时外逐字节相同，数据见 [a5-t300-arms](../validation-data/task-type-evolution-v1/a5-t300-arms.json)），
  两组都有效、用量 exact：

  | 配对 | Single | Multi | Single token | Multi token |
  |---|---|---|---|---|
  | A5 r1 | 失败 | 失败 | 162k | 696k |
  | A5 r2 | 失败 | **通过** | 145k | 575k |

  - 在这 2 组有效配对上，A5（强依赖）是 Multi 1/2、Single 0/2，Multi token 约为 4.0–4.3 倍。
    与事前预期“Single 更好”不符。每臂只有两次，一次通过的差异不足以下强结论。
  - 这次 110 次调用最长 34s，没有一次超过原 120s，300s 超时未被触发。
    这两组有效是因为本轮模型没写出长回答，不能当作 ADR-0086 的效果证据。
- Single 在 A4 通过而 tta2 为 0/2。本轮源码改动都不作用于 Single，这是同配置下的轨迹差异。
- 超时的真实根因见 §5（本地请求超时短于满额输出时间，不是供应商挂住）。补跑按预登记修正各一次，不再重复。

## 3. 实验 B：Multi 上下文治理

预登记见 [preregistration-b](../validation-data/task-type-evolution-v1/preregistration-b.json)，逐臂数据见
[b-arms](../validation-data/task-type-evolution-v1/b-arms.json)（以 `condition` 字段为准，变体 id 在两个条件下都
写作 `ungoverned-1/2`，是计划生成器的命名问题）。

选择规则未满足：实验 A 所有 Multi 臂峰值 34,529 < 96k 触发线 51,456，折叠 0 次。按预登记回退：A2 在 48k 窗口
（input_limit 37,760，触发 22,656）开治理 vs 全部角色不配 `context_policy`，其余配置逐字节一致，各跑 2 次。

| 条件 | 固定验收 | 主方输入峰值 / 均值 | 主方请求 | 折叠 | 读回 | 折叠后重跑同一调用 | 工具调用 | 全树 token | 墙钟 s |
|---|---:|---|---:|---:|---:|---:|---:|---|---|
| 开治理 48k #1 | 失败 | 27,262 / 18,217 | 24 | 22 | 0 | 11 | 47 | 532k | 607 |
| 开治理 48k #2 | 失败 | 未知（1 次在飞取消）；已测 22,069 | 27 | 25 | 0 | 4 | 59 | 未知 | 623 |
| 关治理 #1 | 通过 | 32,730 / 21,583 | 23 | 0 | 0 | 0 | 34 | 528k | 430 |
| 关治理 #2 | 通过 | 33,610 / 23,402 | 24 | 0 | 0 | 0 | 29 | 595k | 560 |

助手 `patch_author` 峰值 5–15k，任何条件都没有折叠；两个条件都没有超限或上下文长度错误。

失败机理（逐事件核对主方 Session）：

- 四次运行的首份 `FINDINGS.json` 都犯同一个模型错误：把 `brain_uuid.py` 中 `register_transform` 的第三个参数
  （lambda 谓词）当成 transform。这发生在任何折叠之前，与治理无关。
- 关治理：#1 在交付前自查发现并改正；#2 被验收退回一次后重读 `brain_uuid.py` 改正，都在期限内通过。
- 开治理：自查和修复阶段，原来读过的源码结果已被折叠。占位符写明“read to reopen, do not rerun”，并给出
  `read_tool_output` 参数，但读回 0 次；主方改为重跑 `read_file`，#2 还额外发出 24 次 `search_text` 来重建事实。
  #1 在第一次交付前还漏了 `integrate_child_patch`。两次都在修复中途因 Turn 期限（约 600s）被取消，没有第二次交付。

结论（2 对 2，仅本题本模型）：

- 折叠确实降低了单请求输入：主方均值约低 16–22%，峰值约低 17–19%。
- 但在这个窗口下质量出现回退：0/2 对 2/2。
- 全树 token 没有节省：532k 对 528k / 595k。
- 回退不是因为折叠删掉了答案，而是被折叠的源码在自查/修复时要重新获取；模型不走读回，重跑原工具，
  这耗尽了期限。

因此当前 48k/60% 这组治理配置不能宣称“质量不降”。可观察的改进方向是在宿主侧让重跑命中折叠结果，或保护
小体积的源码读取结果；这属于新决策，本收口计划不实现。

### 3.1 B2：修复“折叠后重跑再被折”之后的同条件复测（ADR-0084）

逐事件核对 B 的开治理运行发现：模型不走读回，而是折叠后重跑原 `read_file`。
这些重跑结果按原规则是普通候选，还会被再次折叠。
[ADR-0084](../adr/0084-rerun-after-fold-is-reopen-demand.md) 把“折叠后重跑同一调用”计为重开需求：
在原读回字节预算内保护该调用的最新副本，结果仍可见时的重复不算。定向测试与两项反向验证通过，相邻 36 个文件、817 项回归通过。

预登记见 [preregistration-b2](../validation-data/task-type-evolution-v1/preregistration-b2.json)，
数据见 [b2-arms](../validation-data/task-type-evolution-v1/b2-arms.json)。源码摘要 `0ec510ff…`，
与 B 的唯一差别是 `session/surface_replacement.py`，计划、题库、模板、模型、预算逐字节相同。

- **第 1 次尝试**（`ttb2-gov`）：第 1 臂第 5 次请求 `provider-timeout`，发生在任何折叠之前（0 次折叠，
  1 次用量未知），按臂间停止规则第 2 臂未启动。
- 按事先写入的修正复跑一次（`ttb2b-gov`），并约定：无论结果如何都计入；若再遇 Provider 失败即停止。

| 条件 | 固定验收 | 首份交付 | 验收退回 | 主方峰值 / 均值 | 折叠 | 读回 | 折叠后重跑（其中再被折） | 全树 token | 墙钟 s |
|---|---:|---|---:|---|---:|---:|---|---|---|
| 旧开治理 #1 | 失败 | 漏整合 + uuid 错误 | 1 | 27,262 / 18,217 | 22 | 0 | 11（4） | 532k | 607 |
| 旧开治理 #2 | 失败 | uuid 错误 | 1 | 未知 | 25 | 0 | 4（0） | 未知 | 623 |
| **B2 开治理 #1** | **通过** | uuid 错误 | 1 | 28,816 / 20,331 | 19 | 0 | 12（2） | 524k | 587 |
| **B2 开治理 #2** | **通过** | 正确 | 0 | 23,333 / 17,670 | 12 | 0 | 0（0） | 297k | 347 |
| 关治理 #1 | 通过 | uuid 错误 | 0（自查修正） | 32,730 / 21,583 | 0 | 0 | — | 528k | 430 |
| 关治理 #2 | 通过 | uuid 错误 | 1 | 33,610 / 23,402 | 0 | 0 | — | 595k | 560 |

如何解读（2 次，只限本题本模型）：

- 开治理的固定验收从 0/2 变为 2/2，但**不能把 2/2 全部归因于修复**：
  - B2 #2 首份交付就正确，没有进入修复循环，也没有折叠后重跑，新机制未被触发，这次通过属于模型轨迹差异。
  - 真正走到同一路径的是 B2 #1：它与旧 #2 一样首份交付犯 uuid 错误、被验收退回一次。
    这次在新机制作用下（12 次折叠后重跑中，只有 2 份较旧副本被再折）在期限内修复并通过，而旧 #2 在这条路径上超时失败。
  - 这与“修复打断了颠簸”一致，但只有 1 例，不是因果证明。
  - 旧 #2 的 4 次重跑并未被再折，它的失败还包含改用 24 次 `search_text` 重建事实的开销；
    因此 ADR-0084 只消除了已识别的一条路径，不是全部原因。
- 与同样走修复循环的关治理 #2 相比，B2 #1 在固定验收相同（通过）的前提下：
  - 主方单请求输入均值 20,331 对 23,402（−13%），峰值 28,816 对 33,610（−14%）；
  - 全树 token 524k 对 595k（−12%），墙钟 587s 对 560s（+5%）。
- 两次 B2 合计对两次关治理：均值约 19.0k 对 22.5k（−16%），全树 token 821k 对 1,123k（−27%）。
  后一项主要来自 B2 #2 更短的轨迹，不应当作折叠的节省。
- 所有 B2 用量 exact；两个条件都没有超限。

## 4. 实验 C：Single 有界自进化

预登记见 [preregistration-c](../validation-data/task-type-evolution-v1/preregistration-c.json)，开发逐臂数据见
[c-dev-arms](../validation-data/task-type-evolution-v1/c-dev-arms.json)，建议选择见
[c-selection](../validation-data/task-type-evolution-v1/c-selection.json)。

### 4.1 开发运行（`C:/Users/caojie/ttc-dev`，源码摘要 `d16fcdde…`）

| 开发题 | Single A/A | 整树 token | 墙钟 s | 主方峰值 | 失败机制 |
|---|---:|---|---|---|---|
| marshmallow-1359 | 0/2 | 496k / 520k | 595 / 601 | 17.8k / 19.7k | Turn 正常完成、交付补丁，固定验收不通过 |
| astroid-1196 | 0/2 | 265k / 307k | 349 / 342 | 15.8k / 15.7k | 同上 |
| pvlib-1154 | 2/2 | 139k / 176k | 225 / 252 | 12.8k / 13.9k | — |

所有用量 exact，无 Provider 失败，折叠 0 次。原比较报告把三份 A/A 配对都标为 `regressed`，
这是原规则“任一成本增量即回退”在同配置两臂上的噪声，两臂质量一致，不代表变化。

### 4.2 后台检测与建议（同一 `BackgroundOptimizationHost`，无头直连）

- 6 次开发运行全部经 `observe_evaluation` 按原验证器重读导入；4 次含检测记录（`tool-failed` 4 个来源，
  `tool-denied` 2 个来源），2 次没有结构信号。
- 第 1 条建议失败，`provider-http-authentication`：无头工具直接构造 Provider，但没有像 `traceh eval` 那样
  加载 `.env`。已修为先用项目 `.env` 加载器、再清代理，反向验证见第 6 节。
  - 宿主按合同把周期阻塞在 `suggestion-convergence-or-usage-unproven`；核实后经 `acknowledge_blocked` 解除，
    原账本保留该次未知用量和已花掉的额度。
  - 这一次消耗了 `tool-denied` 聚类。
- 第 2 条建议针对 `tool-failed`（4 个来源），分析调用 exact 4,625 token。候选只改白名单文本
  `CODER_GUIDANCE`，追加一句：“工具执行失败时先诊断错误、调整命令或环境并重试，再报告”。
- 核对原证据：被引用的 7 次失败**全部**是 `shell` 在沙箱 `start-failed`。模型写了 `PYTHONPATH=src python …`、
  `cd … &&`、`| head` 等 shell 语法，而工具描述已写明按 argv 执行、不解释这些语法。
  检测摘要只向分析模型提供“N 次工具失败”，不带错误类别，所以建议是泛化的“诊断并重试”，没有点名语法误用。
- 开发题的主要失败是“交付了但修错”。按预登记隔离，检测器只看 Session 结构信号，不看验收结果，
  因此这一机制不在建议的覆盖范围内。
- 用户选择用该建议跑验证。

### 4.3 验证与采用判定

3 道验证题 × 2 次基线/候选配对（首臂交替），按预登记采用门判定。候选臂源码摘要 `285a3bbb…`，
其 `product/execution.py` 含建议文本、基线臂不含，逐配对已核对。逐臂数据见
[c-val-arms](../validation-data/task-type-evolution-v1/c-val-arms.json)；所有已完成配对用量 exact、无未知 Attempt。

| 验证题 | 基线 | 候选 | 基线 token | 候选 token | 工具调用 基线 / 候选 |
|---|---:|---:|---|---|---|
| astroid-1978 r1 | 失败 | 失败 | 115,826 | 155,368 | 15 / 16 |
| astroid-1978 r2 | 失败 | 失败 | 129,972 | 143,458 | 16 / 18 |
| pyvista-4315 r1 | 失败 | 中断缺臂 | — | — | — |
| pyvista-4315 r2 | 通过 | 通过 | 389,559 | 527,805 | 26 / 28 |
| marshmallow-1343 r1 | 通过 | 通过 | 313,947 | 238,340 | 28 / 25 |
| marshmallow-1343 r2 | 通过 | 通过 | 562,140 | 448,001 | 43 / 29 |
| 合计（5 组完成配对） | 3/5 | 3/5 | 1,511,444 | 1,512,972 | 128 / 116 |

执行经过：

- 第一批用了单一 `--benchmark`。astroid 两份正确运行；pyvista 两份因计划锁定的题库摘要不符，
  在任何模型调用前被拒（`evaluation-manifest-invalid`，0 token），批次按连续失败规则停止。
  之后按各自材料分批补跑。
- pyvista r1 在补跑中被外部终止：Claude Code 在系统内存不足时回收了后台进程，不是程序失败。
  已记录的 29 次 Attempt 都有结束事件，但终止之后是否还有在途请求无法证明，因此记为中断缺臂，不补跑、不计入。

采用门（预登记）判定：

- 没有“候选失败而基线通过”的题：成立。
- 通过数没有增加（3/5 对 3/5）。
- 整树 token 比为 1.001，高于 0.95。
- 结论：**不采用**。

分题看，token 比在 astroid 为 1.34 / 1.10、pyvista 为 1.35、marshmallow 为 0.76 / 0.80，方向不一致。
开发集同配置 A/A 两臂的差异已达 1.27（pvlib 139k 对 176k），所以这些分题差异在噪声范围内。

机制核对：建议针对的是 shell 启动失败，但这一机制没有减少。5 组完成配对中，`start-failed` 在基线为 3 / 62 次
shell 调用，在候选为 4 / 48 次。

### 4.4 实验 C 结论

- **检测与建议链路稳定可用**：
  - 6 次开发运行都经原验证器重读导入；
  - 聚类只在 ≥2 个来源上准入；
  - 建议只改白名单文本；
  - 失败的一次按合同阻塞、经人工核实解除，未知用量保留；
  - 人工选择后，由原 paired `traceh eval` 验证，由原采用门判定。
- **本轮唯一的建议未通过采用门**。根因不在执行链路，而在检测给分析模型的信息量：
  - 只有“N 次工具失败”这样的计数，没有错误类别，建议因而泛化，没能点名 shell 语法误用；
  - 开发题的主要失败“交付但修错”，按预登记隔离不在检测范围内。
- 若要继续改进，下一步是让检测摘要携带有界的错误类别（如 `start-failed`），这是对 ADR-0082 检测合同的新决策，
  本收口计划不实现。

### 4.5 C2：检测带上宿主记录的失败原因后重做（ADR-0085，源码摘要 `bbe085dd…`）

预登记见 [preregistration-c2](../validation-data/task-type-evolution-v1/preregistration-c2.json)，选择记录见
[c2-selection](../validation-data/task-type-evolution-v1/c2-selection.json)，验证数据见
[c2-val-arms](../validation-data/task-type-evolution-v1/c2-val-arms.json)。
开发证据仍是 §4.1 的 6 次运行，新开一个后台周期，白名单、聚类门槛、验证题与采用门不变。

**建议阶段**（两次分析调用，均 exact）：

- 第 1 条针对 `tool-denied`（2 个来源，4,497 token），要求只调用当前步骤公开的工具；
  用户选择关闭，让宿主处理排队中的 `tool-failed` 聚类。
- 第 2 条针对 `tool-failed`（4 个来源，5,635 token），理由直接写出“执行环境把 `cd` 和
  `PYTHONPATH=src` 当可执行文件运行，导致 FileNotFoundError”，建议文本要求运行检查时直接调用可执行文件，
  不用 shell 内建命令或行内环境变量赋值。上一轮同一聚类得到的是“诊断并重试”。用户选择验证这一条。

**验证阶段**：

- 首批 6 组中 3 组的基线臂遇到 `provider-timeout`，候选臂按规则未启动。
- 按预登记修正，这 3 组各补跑一次（`ttc2b-val04/05`），全部有效。
- 数据见 [c2-val-arms](../validation-data/task-type-evolution-v1/c2-val-arms.json) 与
  [c2-val-redo-arms](../validation-data/task-type-evolution-v1/c2-val-redo-arms.json)。

| 验证题 | 基线 | 候选 | 基线 token | 候选 token | 工具调用 基线 / 候选 |
|---|---:|---:|---|---|---|
| astroid-1978 r1 | 失败 | 失败 | 107,826 | 104,693 | 15 / 14 |
| astroid-1978 r2 | 失败 | 失败 | 213,236 | 128,410 | 20 / 14 |
| pyvista-4315 r1（补跑） | 失败 | **通过** | 413,983 | 783,384 | 29 / 38 |
| pyvista-4315 r2 | 失败 | **通过** | 399,826 | 440,577 | 27 / 27 |
| marshmallow-1343 r1（补跑） | 通过 | 通过 | 406,520 | 306,471 | 33 / 27 |
| marshmallow-1343 r2（补跑） | 通过 | 通过 | 350,039 | 209,134 | 30 / 21 |
| 合计 | 2/6 | **4/6** | 1,891,430 | 1,972,669 | 164 / 141 |

采用门（预登记）判定，6 组全部有效：

- 没有“候选失败而基线通过”：成立。
- 通过数 2 → 4：成立。
- 整树 token 比 1.043，工具调用 −23。
- **结论：满足采用门。**

如何解读：

- 增益全部来自 pyvista（候选 2/2，本轮基线 0/2）。
- pyvista 基线跨轮合计 1/4（上一轮 C 为 1/2，源码 `d16fcdde`），所以这是“候选 2/2 对基线 1/4”的单题信号，
  每臂只有两次，仍可能包含轨迹波动。
- 候选在 marshmallow 上更省（0.75 / 0.60 倍），在 pyvista r1 更贵（1.89 倍）。
- 机制上，shell `start-failed` 基线 4 / 63 次（6.3%）、候选 2 / 50 次（4.0%）。方向一致，但计数太小，不作证明。

结论：

- ADR-0085 让建议从泛化变为点名真实原因。
- 该建议在完整的 3 × 2 验证上满足预登记采用门：通过 2/6 → 4/6、没有回退，这是本项目第一条通过采用门的后台建议。
- 样本小且增益集中在一道题，只能说“验证通过”，不能说“普遍提升”。
- **已人工采用**（用户批准，[ADR-0087](../adr/0087-adopt-background-coder-guidance.md)）：
  用原 `apply_candidate` 核对 old_sha256 与 AST 后写入 `CODER_GUIDANCE`，字符串值与候选逐字节相同。
  候选绑定的 `bbe085dd` 与当前源码只在 ADR-0086 的三个评测文件上不同。
  至此，“检测 → 受限建议 → 人工选择 → 配对验证 → 采用门 → 人工采用”完整走通一次。

## 5. Provider 超时的根因（ADR-0086）

实验期间共 7 次 `provider-timeout`，全部等满 120s。原先判断为供应商挂住，核对 1,830 次成功调用后推翻：

- 耗时与输出 token 成正比，每千 token 约 17–18s，各小时一致，夜间不慢。
- 最长成功调用 4,290 token 用时 75s，75–120s 之间没有一次成功。
- 输出上限 8,192 token 约需 148s，而请求超时只有 120s。输出超过约 6,600 token 的回答必然被本地截断。
- 超时集中在夜间，是因为那时在跑更难的任务。
- 本机排查排除了代理与网络：域名解析为真实地址，路由走物理网卡，Clash 未开 TUN，没有 Wi-Fi 断连或睡眠事件。
- 这个 120s 来自 CLI 默认值或环境变量，**不在运行计划和冻结条件里**。

[ADR-0086](../adr/0086-frozen-model-request-timeout.md) 让运行计划必须冻结 `model.timeout_seconds`，
Runner 校验 Provider 实际值，`frozen.json` 记录实际值；本项目实验与 TUI 生成的计划取 300s。
重试与放宽停止规则都被否决：重试只会把同样长的生成再跑一遍后再次超时。
本轮已有结果都是在 120s 下得到的，保持原样；A5 的多次超时属于这一根因。

## 6. 本轮修复与验证

- ADR-0083（分工计划主方工作拆平）与分工字段拒绝写明缺少/不允许的字段：定向测试与反向验证通过
  （正式版 14.3.1、14.3.26）。
- 实验 C 无头 `suggest`（`tests/real_repository_evaluation/evolution.py`）：
  - 先用项目 `.env` 加载器、再清除 `*_PROXY` 并安装空 `ProxyHandler`；新增 `--acknowledge`，走宿主原
    `acknowledge_blocked`。
  - 新测试 `test_the_suggest_step_loads_its_key_from_env_and_still_connects_directly`。反向验证：把加载移到清代理之后，
    测试因 `.env` 中的 `HTTPS_PROXY` 残留而失败；恢复后 `test_evolution_tooling.py` 6 项通过，Ruff 通过。
  - 这是研究工具修复，不改生产代码。
- ADR-0084（折叠后重跑计为重开需求）：
  - 新增 3 项定向测试：重跑副本被保留；预算为 0 时照常折叠；仍可见时的重复不算需求。
  - 两项反向验证各自按预期根因失败：去掉重跑保护、把“折叠后”条件改为“任何重复”。
  - 一个既有用例的填充读取改为不同文件，原意不变。
  - 相邻 36 个测试文件、817 项通过。
- ADR-0085（评测证据的检测附宿主记录原因）：
  - 新增 5 项定向测试。
  - 三项反向验证各自按预期根因失败：去掉原因提取、去掉标识改写、去掉聊天/Product 的范围限制。
  - 后台、TUI、策略、架构、工具链等相邻测试通过。
- 集成检查点：源码摘要 `bbe085dd…`（A4/A5 重跑与 C2 所用源码），`compileall` 通过，
  完整 `pytest -q --durations=30` 结果为 4135 passed / 305 skipped / 0 failed / 0 error，含真实 L2 门禁（1730s）。
  修改文件的 Ruff 通过。`ruff format --check` 在 HEAD 上即不通过，属既有格式漂移，未在本轮处理。
- 本轮没有提交、推送或发布。
- ADR-0086（冻结请求超时）：
  - 新增 11 项定向测试，另把 `--model-timeout-seconds` 加入“计划模式拒绝命令行覆盖”的参数化。
  - 四项反向验证各自按预期根因失败：去掉取值校验、不把计划值交给 Provider、去掉 Runner 一致性校验、不写入冻结条件。
  - 一处多余保护经检查不能触发任何失败，已删除，依据 AGENTS §8.3。
  - 评测计划相关 26 个测试文件 238 项通过。
- 最终集成检查点：源码摘要 `9dcaf759…`（含 ADR-0086，是 A5 300s 重跑所用源码），
  完整 `pytest -q` 结果为 4146 passed / 305 skipped / 0 failed，含真实 L2 门禁。
- ADR-0087（人工采用 `CODER_GUIDANCE` 候选）：
  - 受影响的 Product、候选、后台与工具链测试 376 项通过。
  - 采用后源码摘要为 `7eb26db5…`，最终完整 `pytest -q` 结果为 4146 passed / 305 skipped / 0 failed。
