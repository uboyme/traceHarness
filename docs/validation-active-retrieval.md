# 主动检索 AR-A–AR-D 实现与验收

**2026-09-10 发布决定：** 用户接受 55/72 与已知限制，授权 v0.9.0 Educational alpha 发行并转入沙箱 S0。原冻结门槛和历史 NO-GO 不改写，但不再阻断本次发行；见[收口记录](deal/011-v090-release.md)。

**2026-09-10 最新补测后成绩：55/72（76.4%）＝51＋4。** 只直连补测原六条候选网络失败题，四条通过、两条回答/依据问题，基线未重跑；见[记录 010](deal/010-grid06-direct-supplement.md)。下文 grid-06 的 51/72 为原始完整运行记录，原门槛 NO-GO 保留。

日期：2026-09-09。**AR-A–AR-D 及后续定向修复已执行；最新完整真实网格 grid-06 为 20/72 → 51/72，原门槛仍为 NO-GO。** 最终测量见第 6 节与[记录 007](deal/007-active-retrieval-final-comparison.md)；第 3–5 节保留 grid-05 历史证据。

后续 RE 可靠性实验已收口：112 条真实目标旅程，无稳定收益候选，RE-5 留出验证因无合格组合未启动；见[记录 008](deal/008-retrieval-reliability-experiments.md)。本页历史网格成绩与发布结论不变。

前序工作已提交为 `a54d431`，未推送。本次 AR 改动纳入 v0.9.0 本地发行提交。
执行边界见[计划](plan/TRACEHARNESS_ACTIVE_RETRIEVAL_EXECUTION_PLAN.md)及 [ADR-0061](adr/0061-active-reference-search.md)。

## 1. 已实现的主线

- AR-A：冻结自然问题、材料生成、资源上限、模型身份和判定门槛；没有用评分结果修改问题或预期答案。
- AR-B：`search_history` 从当前 Session 原 History Reader 查字面关键词，返回有界片段及合法原文页动作。
- AR-C：`search_memory` 查询当前项目已批准的有效事实；`search_skill` 查询宿主选择的 Skill 导航，包括章节和资源分块。
  命中仍经过原 Reader、批准/选择、版本、Lease、取消及 Context 预算检查；搜索本身不授予新权限。
- AR-D：Line/TUI 显示搜索来源、覆盖范围和失败状态；执行基线/候选真实对照，逐项审阅实际派发证据，独立重开账本重放。

```mermaid
flowchart LR
    FACT["原事件 / 内容"] --> READER["领域 Projection / Reader"]
    READER --> SEARCH["有界字面搜索：候选与合法读回动作"]
    SEARCH --> CONTEXT["原 Context：当前状态校验与预算准入"]
    CONTEXT --> REQUEST["冻结请求 → 模型"]
    REQUEST --> READ["原工具请求正文"]
    READ --> READER
```

没有新增事实数据库、向量模型、第二套生命周期或后台 Agent。当前 Session 13 / Context 12；grid-05 当时为 Session 11 / Context 10，各轮按自己的冻结源码重放。旧 Session 明确拒绝，
不改写用户原会话和 Profile。语义检索保持关闭。Skill 首版只搜索导航元数据，不搜索尚未披露的正文。

## 2. 真实测试发现并修复的两个问题

**搜索参数上限没有告诉模型。** grid-02 的模型连续请求 `limit=10`，而冻结配置只允许 6 条；参数报错后误判为没资料。
现按实际配置生成 schema 的 maximum/default 和说明，错误明确提示修改参数，不能把它当作无命中。
移除上限说明，公开派发测试就从找到证据变成没有证据；恢复后通过。见 [grid-02 中止记录](validation-data/active-retrieval/ar-d-grid-02/status.json)。

**大结果收存清空了 Skill 控制回执。** grid-03 的 Skill 请求成功，但附带整份导航超过输出阈值，Result.data 被清空；
下一 Context 因而不知道要披露正文。现沿原 Effect → Session 呈现路径保留宿主参考工具的成功控制回执，
其余大 data 仍在原 Effect。回执投影必须与原 payload 相等，普通工具的同名字段不会得到这一待遇。
三个来源在 64 字符测试阈值下均完成搜索及原文读回；删除保护会在实际搜索后的 Context 冻结处报回执错误。
撤销、取消选择和重复取消仍阻止后续准入。见 [grid-03 中止记录](validation-data/active-retrieval/ar-d-grid-03/status.json)。

两轮中止的数据、失败和未完成调用均保留；它们不是完整评分轮。grid-04 使用完全相同的题目、材料、预算和门槛。

## 3. 历史 grid-05 完整网格：未通过

[grid-05 冻结配置](validation-data/active-retrieval/ar-d-grid-05/frozen.json)：同一 `openai-compatible/qwen-plus`，
24 道自然问题、三组种子、两套合成领域材料，每臂 72 条。目标问题不包含工具名、内部 ID、预期答案或操作步骤。
原 Runtime、SQLite、Tool、Skill 贡献/选择、Memory 批准路径实际运行；没有脚本回答冒充模型。
grid-04 曾有 117/144 条 TLS EOF，仍保留其 [NO-GO 统计](validation-data/active-retrieval/ar-d-grid-04/summary.json)。
无评分连接探针恢复后才开始 grid-05，预先固定两组相同的两批安排，每批最多两个 worker；题目、材料、模型、预算和门槛不变。
没有证据证明并发就是此前 TLS 的根因。

| 指标 | 基线 a54d431 | AR 候选 |
|---|---:|---:|
| 任务记录 | 72 | 72 |
| 完成目标回答 / 耗尽 Step | 69 / 3 | 69 / 3 |
| 连接错误 | 0 | 0 |
| 答案与实际证据联合通过 | **21/72** | **45/72** |
| History | 6/18 | 13/18 |
| Skill | 0/18 | 14/18 |
| Memory | 12/18 | 14/18 |
| Tool Output | 3/18 | 4/18 |
| 明确误报所问事实 | 9 | 8 |
| 独立重放请求 | 469 | 440 |
| 重放 / 不变量错误 | 0 | 0 |

候选没有达到 **66/72**，四类也都没有达到 **15/18**，且违反无证据编造测试事实必须为 0 的门槛。
实际资料越权、重复工具副作用、旧 Memory 冒充当前批准均未观察到；这不等于问答可靠性已经达标。

具体例子（均为合成测试材料，绝非产品默认）：

- “档案复核组的联络代号是什么？别和搬运组混了。”候选搜索历史，读到正确组的记录，三组种子均回答正确。
- 手册交班暗语和应急联络卡可以经原 Reader 读回正文。收益包含控制回执修复，不能全部归因于新增搜索。
- “刚才失败的归档检查给出的故障代号是什么？”候选三次都把退出码 `7` 当故障代号，没有读到输出深处的实际业务代号。
- “刚才归档检查返回的那串核验代号是什么？”候选三次都误用了输出 `digest`，而非源正文里的核验代号。
- 自动目录遗漏的 Memory：有时根本不搜索；有时先读背景项、再分页，用完步骤前仍没找到目标。不能因此抬高冻结预算。
- “没有这条资料”的负例常只检查局部便断言全局不存在；Skill 导航搜索无命中也不等于正文不存在。

候选存在 14 条预先标注发现类任务的成对增益；另有 3 条基线通过、候选失败，逐条列在统计中。
这些是本次固定配置的观察，不是通用成功率保证，也不足以单独证明确定性代码回归或因果收益。
目标正确且有证据的两条负例仍附带错误总行数，已记录额外事实错误；联合目标通过不表示整段回答完全准确。

[逐项判定](validation-data/active-retrieval/ar-d-grid-05/manual-review.json)、
[实际派发证据](validation-data/active-retrieval/ar-d-grid-05/review-packets.json)、
[分项统计](validation-data/active-retrieval/ar-d-grid-05/summary.json)可逐题核对。
中文/英文、Step、搜索读取次数及延迟均保留；性能只统计实际完成回答的 69/69 条，缺失值不补零。
worker 的零填充均值不用于最终结论。

## 4. 历史 grid-05 验证与用量

- 最终 owner/相邻检查 145 项、协议/CLI/合同 143 项，合计 **288 项不同用例通过**。
  包含正文读回、来源反例、撤销/取消选择、重复取消、大结果、失败持久化、重放和界面显示。
- `compileall src tests`、43 个修改/新增 Python 文件 Ruff、仅收集 3616 项和 `git diff --check` 通过。
  字符串格式换行后的 4 项 schema 确认属于重复子集，不额外计入 288。
- 新 SQLite 备份与独立 SessionService/SurfaceProjector 重开两臂 144 个会话，**909 个冻结请求精确重建**，
  原事件一致、重放/不变量错误 0、未结束 Attempt 0；不启动 Provider。
  见[基线重开](validation-data/active-retrieval/ar-d-grid-05/baseline-reopen.json)和
  [候选重开](validation-data/active-retrieval/ar-d-grid-05/candidate-reopen.json)。
- grid-05 **909 个已结束 Attempt，4032845 total_tokens**；基线 1973550，候选 2059295，均含准备材料。
  本轮 unknown/estimated usage 为 0；中止轮与 grid-04 的未知用量仍标 unknown，不能当实际零。
  无评分连接探针另用 2985 tokens；AR-B/C 诊断另计，均不计验收得分。见[成本账](validation-data/active-retrieval/ar-d-costs.json)。

grid-05 的 `candidate-source.zip` 保存与冻结摘要逐文件匹配的源码和支持脚本；运行期间未修改它们。
grid-04 后仅一处超长字符串换行，AST/字符串相同，见[等价确认](validation-data/active-retrieval/ar-d-post-freeze-formatting.json)。
文档检查见[证据](validation-data/active-retrieval/ar-d-doc-qa.json)。
没有运行全量 pytest、L2–L4、Wheel、安装或发布门禁；没有 push/tag/release；没有改用户 Profile/Session。
真实连接仅经已授权配置加载器，没有输出或写入真实密钥。

## 5. grid-05 收口与后续授权

本次完成计划规定的实现、范围验证和完整真实验收执行，**验收结论 NO-GO，不是功能质量全部通过**。
主代理范围审查未发现仍成立的当前 P0/P1；这是范围审查，不冒充独立 Agent 审查。
两项公开链路缺陷已修复并反向确认。模型不继续检索、混淆字段、过度断言仍是实测限制。

按冻结停止规则，不进入下一阶段或发布，不无限调整 prompt、换模型、提高预算直到碰巧通过。
后续应另定证据使用和检索行为改进的范围，保留本轮作为比较基线；当前不提前加入语义检索。
唯一事实源、原领域 Reader/Projection、批准/选择、Lease/取消及冻结请求边界保持。
正式与通俗上下文同步 **1、7.11、9.5**，计划、ADR 状态和 CHANGELOG 对齐；AR 改动未提交。

## 后续诊断说明

用户随后授权的职责归类与小规模真实对照见[诊断记录](deal/002-active-retrieval-responsibility-diagnosis.md)。
两项呈现实验均未合入生产，不修改上述 AR-D 冻结分数和 NO-GO 结论。

## 6. 当前最终复测 grid-06

用户随后授权的来源导航、重复拒绝和证据呈现修复已完成，最终保持生产代码不变再测原网格一次。基线仍为 `a54d431`，相同问题、材料、模型、预算和门槛；没有跨轮择优或补跑失败。

| 指标 | 基线 | 当前候选 |
|---|---:|---:|
| 联合通过 | 20/72 | 51/72 |
| History / Skill / Memory / Output | 5 / 0 / 11 / 4 | 12 / 14 / 14 / 11 |
| 完成回答 / 用尽步骤 / TLS EOF | 58 / 3 / 11 | 66 / 0 / 6 |
| 两臂无执行错误的 57 对 | 19/57 | 44/57 |
| 独立日常对照正确数 / 工具调用 | 4/4 / 0 | 4/4 / 0 |

固定分母增加 31 题、43.1 个百分点；当前比 grid-05 候选多 6 题。主网格 837 个请求、独立对照 8 个请求均重放通过，无实际越权、重复副作用或旧批准冒充当前的观察。仍有 4 条错误事实，以及局部未命中就否定整个来源的行为，原门槛 NO-GO。四个日常对照没有观察到过度搜索，不代表任意场景保证。

本轮已报告 3671880 tokens，另有 153 次 Attempt 用量未知，不能按零计。完整分项、逐题证据、用量及验证边界见[记录 007](deal/007-active-retrieval-final-comparison.md)和[grid-06 统计](validation-data/active-retrieval/ar-d-grid-06/summary.json)。本轮只验证与同步文档，没有继续调生产策略，也没有运行全量、L2–L4 或发布。
