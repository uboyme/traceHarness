# 081：080 方案 C0–C3 的实施与 C0-2 真实探测

日期：2026-09-21。状态：**C0–C3 已有实现与阶段验证记录；C0-2 真实形状探测已执行；C4 真实主线验证见 §6。**
同日复核发现终态报告的及时停止未实现、run3 不能归因为“必需证据约 66k”；
相关结论已在本文纠正，独立日志核对与待修复边界见[记录 082](082-c4c-context-and-late-stop-diagnosis.md)。下文原阶段测试记录不代表此次重新执行。

承接[方案 080](080-long-task-context-and-completion-design.md)。本记录只陈述已经做成并可复核的事实，
以及明确未做的部分；设想留在 080，不在此改写成当前能力。

## 1. C0-2 的探测结果改变了根因认识

这是本轮最重要的单条事实。方案 080 §4.0 把"推理通道吃掉输出额度"列为**待查假设**，
并明确禁止提前定为根因。探测已执行：

- 1 次调用，百炼 `deepseek-v4.1-flash`，非流式，`max_tokens=64`。
- 脚本：[`tests/provider_response_probe/probe.py`](../../tests/provider_response_probe/probe.py)。
- 证据：[`c0-response-shape-probe.json`](../validation-data/real-repository-pilot-v1/c0-response-shape-probe.json)。

观察到的返回形状与 v10 的签名一致：

| 观察项 | 值 |
|---|---|
| `choices[0].finish_reason` | `length` |
| `message.content` | 长度 0，空白 |
| `message.reasoning_content` | 长度 241，非空 |
| `message.tool_calls` | 无 |
| `usage.completion_tokens` | 64 |
| `usage.completion_tokens_details.reasoning_tokens` | 64 |
| `usage.completion_tokens_details.text_tokens` | 0 |

即：**整份输出额度都花在了当时 adapter 根本不读取的推理通道上，正文一个字也没开始写。**
旧 adapter 只取 `message.content`，于是宿主看到一个"空回答"。v10 的日志明确记录了 `length`，
被记成 `completed` 的问题在 Runtime 没有据结束原因拦截；不能把这次错误归因于“缺失值补 stop”。
缺失/未知值的规范化属于另一条已修复的 Provider 边界。

边界（必须保留）：

- 一次调用只描述这一次响应。它**不追认** v10 那一次的字节，历史运行没有保存 HTTP body。
- 探测把输出上限从试次的 8192 降到 64 以便廉价复现截断，这是**已披露的偏离**；
  它因此不测量试次原配置下的截断频率。
- 推理通道存在，不等于"v10 的答案丢在了那里"。它只把假设从"无从判断"变成"机制已在同一模型上复现"。

直接后果：C1 让这种运行如实失败而不是冒充成功，但**它不会让这类任务变成成功**。
C4 若沿用同一输出配置，很可能再次没有交付；先解决有证据的交付瓶颈，再谈上下文治理收益。

## 2. C0-1：结束分类与用量分项

`ModelResponse` 现有 `completion: CompletionCategory`（`normal` / `tool_handoff` / `length` /
`refusal` / `unknown`）与并列可空的 `provider_finish_reason`。Runtime 只消费前者。

关键取舍：**契约钉在 adapter 边界，而不是 dataclass 默认值。** 全仓库 428 处 `ModelResponse(`
构造中只有 6 处在 `src`，其余 85 个测试文件依赖默认值，且仓库没有共用的 conftest 工厂。
若把严格性放在类型上，就是 85 个文件的机械大改；放在 adapter 上则是 6 处，且真正要防的
"供应商没说清楚却被当成成功"完全被覆盖。`openai_compatible` 只认本协议定义的五个值，
缺失、空串、未映射拼写一律 `unknown`；Scripted 是宿主自撰替身，省略时按有无 tool_calls 解析，
未知类别名直接报错。

`Usage.reasoning_tokens` 记录供应商声明的推理分项，是 `output_tokens` 的子集，不改变任何结算；
未提供为 `None`（未知，不是零）。推理正文仍不读入 Session。

持久事件 `model/attempt-end` 与 `summary/response` 改为 `completion` + `provider_finish_reason`，
旧 `finish_reason` 字段不再产生也不被接受。

反向验证：把 `_FINISH_REASONS.get(finish_reason or "stop", NORMAL)` 恢复回去，
8 个参数化用例中恰好"空串 / 别家拼写 / 缺失"3 个失败，其余 5 个仍通过。

## 3. C1：截断与空交付不再算完成

新增 [`runtime/response_completeness.py`](../../src/traceh/runtime/response_completeness.py)，
`judge_response()` 每个响应判一次，AgentLoop 在**工具执行与 Verifier 之前**调用，
continuation 消费同一结果。四种不完整：`response-truncated`、`response-refused`、
`response-completion-unknown`、`response-tool-handoff-without-calls`。

截断响应即使带可解析的 tool_calls 也**不执行**——那是没写完的意图片段，照做等于拿半句话执行副作用。

Product 侧：身份匹配且正常完成的 `collect` 报告，`statement` 为空或全空白时，
在最终交付检查中判 `CollaborationChildDeliveryEmpty`。**当前未实现 collect 所属 Step 立即停止**，
也不能宣称额外主方模型请求为 0；`decide` 仅在最终完成分支调用该检查，具体差距见记录 082。
不自动重派或降级成 single 成功；pending 不算不合格终态。
为此 `collect_investigation` 的 `statement` 进入规范化 `data`（与 `collect_child_patch` 一致），
门禁读到的是与模型所见同一份原始证据，`content` 即 `canonical_json(data)`，没有第二份事实源。

反向验证：移除 loop 的 `may_execute_tools` 守卫与 continuation 的完整性检查后，
"截断空答仍非成功"与"截断响应不执行工具"两个行为用例失败，纯分类用例仍通过（分类本身没坏）；
移除空交付门禁后 `empty-delivery` 用例不再抛出。

## 4. C2：引用资格与首次展示分开

`output_ref` 升到 format 2，新增 `disclosure ∈ {inline, retained}`。只要 outcome 完整，
每条结果都写 `retained_output` 与引用；尺寸只决定首次展示什么。
旧规则按单条结果是否超 24000 字符决定资格，长任务里几百条小结果一条都拿不到引用、
也就一条都折不掉——这正是试次里输入单调增长却没有任何 replacement 的机制。

`output_reference()` 按模式校验不同不变量：`retained` 要求内联 data 已缩减为控制回执，
`inline` 要求内联正文与 data 与存档 payload 逐字节相等，分别以
`tool-output-control-data-mismatch` / `tool-output-inline-payload-mismatch` 拒绝。
format 1 引用不再通过校验，明确拒绝、不转换。

**检索评测口径同步（080 审查的 A3）**：`retrieval_episode.py` 原本用"有没有 output_ref"
筛选输出实验来源，在 format 1 下等价于"宿主保留未展示的大输出"。format 2 会把它悄悄扩成
"所有 shell 结果"，在模型行为不变的情况下改变评分。改为按 `disclosure == "retained"` 筛选后，
精确还原原资格；相关评测器测试恢复通过，72 题口径未漂移。

读回装配：`OUTPUT_TOOL_IDS` 一处命名，`build_default_runtime(output_tool_ids=...)`
让关闭默认工具集的宿主单独授予读回，绑定同一个 SessionService。
这条通路的存在理由是：此前只能整体打开 `include_default_tools`，
那会把 shell 与 apply_patch 一并发给只读调查助手。

## 5. C3：闭合 Step 切口与软硬水位

tool-fold 边界改为 `boundary = {unit, kept_recent}`，`unit ∈ {turn, step}`，
取代原 `kept_recent_turns`（持久协议破坏性变更；`validate_tool_fold`、parser、invariants、
重放与 TUI 读取端同批更新，手动/语义摘要仍只允许 Turn 边界）。

**一次替换只换一条结果**：折 N 条就是 N 次 append/CAS，允许组内半折。
assistant 的 tool_calls、工具名、call id、参数与顺序原样保留，所以半折仍是结构完整的对话，
两次 append 之间中断也没有"半条替换"要修。

`TokenBudgetPolicy` 增加 `fold_relief_percent` 与 `fold_protect_recent_groups`，
**必须同时给出**，都不给表示关闭；低水位必须严格小于高水位。没有通用默认值。

失败语义分离（080 审查的 A6）：Turn 前维护失败记录后继续；
`prepare` 内因高水位要求的折叠失败，记 `surface/compaction-failed(method=step-fold)` 后
**抛出并停止本次模型准入**。"没有合法候选"是有原因的无操作，不是失败。

离线对照（`tests/test_in_turn_step_folding.py`，同任务、同窗口、同六次读取、只改水位）：

| 配置 | 末次请求携带的完整正文份数 | 追加的 Step fold | `turn/end` 数 |
|---|---|---|---|
| 关闭水位 | 6 | 0 | 1 |
| 开启水位 | 1 | 5 | 1 |

全部历史快照独立重放一致、不变量为空。窗口收紧到同一任务装不下时，
关闭水位的一侧按硬上限明确拒绝而非被裁剪。**这是确定性机械收益，不等于真实模型上的节省或同等质量。**

反向验证：移除 prepare 内的 Step 折叠调用后，三个折叠行为用例失败（其中两个直接撞硬上限）。

### 5.1 Product 装配：否则以上全部不生效

实施 C3 后核对试次配置时发现一个独立的、同样致命的原因：Product **从不**向角色 RuntimeConfig 传
`token_budget` / `compaction`，所以 `RequestBuilder.token_meter` 是 `None`，
token 压力压缩与轮内折叠在真实 Product 运行里根本不会触发。用 v10 的冻结证据跑本轮的读数脚本可以直接看到：

| 会话 | 模型调用 | exact 输入 | exact 输出 | replacement | 折叠结论 |
|---|---|---|---|---|---|
| 主方 | 46 | 1,885,301 | 70,706 | 0 | `disabled-no-water-marks-configured` |
| 助手 | 33 | 1,115,140 | 61,566 | 0 | `disabled-no-water-marks-configured` |

（合计约 300 万输入 + 13 万输出，与审计的 3,132,713 exact token 一致。）

因此 `ProductRoleProfile` 增加可选 `context_policy`，经 config/registry 传到 RuntimeConfig，
并进入 assembly digest——同名 preset 但水位不同不会共用一个 digest。
JSON 键可省略（省略即关闭）：设为必填会使磁盘上所有已冻结实验 profile 失效，而那些是不改写的证据；
`_object` 因此区分必填键与允许的可选键，未知键仍然拒绝。

## 6. C4：已执行。改动接上了真实主线，任务失败，并暴露一个新问题

运行完成：`complete=true`、`errors=[]`、不变量通过、收敛通过、95 次模型调用全部 exact usage、零未知用量。
证据：[run1 证据](../validation-data/real-repository-pilot-v1/c4-context-governance-trial-c4-run.json)。

### 6.1 三件被证实的事

| | v10 | C4 |
|---|---|---|
| 主方 | 46 次 / 1,885,301 输入 | 43 次 / 1,104,633 |
| 助手 | 33 次 / 1,115,140 | 50 次 / 1,586,520 |
| 主方单次平均输入 | 40,984 | **25,689（−37%）** |
| replacement | **0** | **156**（助手 132 / 主方 24） |
| `length` 截断 | 有，且被记为 completed | **零** |
| 折叠失败 | — | 0 |

1. **本次没有再发生截断**：输出上限提到 32768 后全程零截断，`completion` 只出现 `normal` / `tool_handoff`；不能由此推断截断问题永久消失。
2. **推理吞噬输出在真实任务上同样成立**：主方 40,624 输出里 24,484 是推理（60%），
   助手 37,616 里 25,893（69%）。C0-2 的单点观察在规模上重现。
3. **轮内折叠真的压住了输入**：主方峰值 36,215（卡在 36,710 高水位下），助手峰值 48,442
   （折叠没完全跟上，但仍在 61,184 硬上限内，未触发拒绝）。

### 6.2 任务为什么失败

`CollaborationChildIncomplete("collaboration-child-report-not-collected")`——**既有的 multi 门禁，
不是本轮新增的 `collaboration-child-delivery-empty`（后者一次都没触发）**。

主方做了实活（26 次 shell、10 次 apply_patch、19 次 read_file），最终报告称已修好
`Dict.getitem` 的 `DictUnpack` 分支。但助手跑满 50 步、耗 162 万 token 仍未完成并被取消；
主方 5 次 collect 前 4 次都是 pending。协作合同要求每个已派发 assignment 都收到 completed 报告才能交付，于是拒绝。

这证明**既有交付门禁最终拒绝了不完整协作**，不证明 C1 的即时停止要求已经实现。
v10 曾把助手截断空答算作完成；本次未把未完成报告算成功。固定 Verifier 没跑到，
**因此主方那个补丁正确与否，本次无法判断**。

### 6.3 新问题：折叠正在诱发重跑原工具

预登记的抖动条件测的是"同一引用被折叠→读回→再折叠"，实测 **0**——而这个 0 没有意义，
因为浪费不是这个形状。真实形状是：**折叠后的结果被重跑原工具重新取回**，
新结果是新的 Effect、新的引用，按折叠身份做的检测器结构上就看不见它。

改用"重复同一工具名 + 完全相同参数，按原结果当时是否已被折叠拆分"后（该度量**是事后补的，
不是通过的预登记门禁**）：

| 角色 | replacement | 读回调用 | 折叠后重跑 | 结果仍可见时重跑 |
|---|---|---|---|---|
| 助手 | 132 | 14 | **22** | 0 |
| 主方 | 24 | 0 | **0** | 6 |

两个角色完全分离：助手的重复**全部**发生在折叠之后，主方一次都没有。
助手自述与调用顺序支持这一解释，但不能单独分离折叠与模型策略的因果贡献：

> `[17] I need to re-read key sources since earlier outputs were folded.`
> `[34] I need the actual Dict.getitem body text (prior reads were folded).`
> `[36] Batching folds older outputs, so I'll read the remaining evidence one or two at a time.`

第 36 步它甚至**为了对抗折叠而退化了策略**——从批量读改成一次读一两个文件。

### 6.4 由此得到的判断

折叠机制可用，但**收益强烈依赖角色的交付形态**：

- **主方**交付补丁，24 次折叠，观察到单次输入 −37%、零折叠后重跑；未完成固定验收，不能称同等质量的净收益。
- **助手**交付的是**带原文引用的调查报告**，折叠恰好收走了它必须逐字复现的东西。
  132 次折叠换来 22 次重跑，单次输入只降 6%，总输入反而更高。

run1 当时考虑过按角色调水位和降低读回成本；后续 §6.5 修改了读回路径，
记录 082 又查明永久保留与占位符累积成本。**“只有配置错误、没有机制问题”的初步判断不成立。**

助手不收敛的成因仍未分离：重跑只占其 110 次读取的 20%，其余 80% 是它在同一批文件里兜圈。
要分开"折叠削弱记忆"和"模型本来就不收敛"，需要同条件关掉水位再跑一臂——本轮未跑。

### 6.5 两个折叠根因的修复与真实复测

§6.3 的现象不是调参问题。顺着证据定位到两条根因，各自修复并做了会失败的反向验证：

**根因一：重开比重跑难。** 折叠占位符原本只给 `output_ref`，模型要自己拼 UUID 加 64 位 digest；
而产生该结果的 assistant 调用就保留在占位符正上方，重跑只需抄现成参数。
同一份 `prepare_tool_output` 给大输出的展示**本来就带 `read_action`**，折叠路径漏了它。
现已补上（只含 reader 的两个必填参数；`part`/`offset` 本就默认整份内容）。
占位符 588 → 814 字符，每次折叠净省 2,172 → 1,946（−10%），
而避免一次重读省约 2,760 字符——12 倍回报。

**根因二：重开的证据两次请求后又被折走。** run1 里 9 次 `read_tool_output` 结果**全部**在 2 次请求后
被再次折叠。折叠一个读回结果把模型刚花一次调用逃离的状态原样重建，而且占位符指向读回工具
自己的输出，一层引用变两层。`tool_fold_plan` 现在整体排除 `OUTPUT_TOOL_IDS` 的结果。

**同条件真实复测**（材料、模型、水位、输出上限全部不变；证据
[run1](../validation-data/real-repository-pilot-v1/c4-context-governance-trial-c4-run.json) /
[run3](../validation-data/real-repository-pilot-v1/c4-context-governance-trial-c4c-run.json)）：

| 助手会话 | run1 修复前 | run3 修复后 |
|---|---|---|
| 模型调用 | 50 | **26** |
| exact 输入 | 1,586,520 | **904,411（−43%）** |
| 折叠 | 132 | 67 |
| 读回调用 | 14（第 40 步才开始） | **28（第 18 次调用时已 15 次）** |
| 折叠后重跑 | 22 | **9** |
| 每次折叠的读回率 | 0.106 | **0.418（4 倍）** |

两方 exact 输入合计：v10 3,000,441 → run1 2,691,153 → **run3 1,940,245（较 v10 −35%）**。
激励方向被扭转过来了：修复前重跑（22）多于读回（14），修复后读回（28）是重跑（9）的 3 倍。

**边界**：这是一次运行对一次运行，模型非确定。run2 在同样条件下走了完全不同的轨迹
（16 次调用、峰值差 1,158 没到触发线、0 次折叠），最后因 `provider-timeout` 中止，
按预登记原样保留、对修复效果无结论。所以上表是**一对配对观察，不是速率**。

**已兑现的已知代价。** 排除读回结果后证据会累积：run3 助手输入估算峰值 65,759，
越过 61,184 硬上限，触发**一次** `RequestTokenBudgetExceeded`，助手 Turn 失败，
主方最终因既有门禁 `collaboration-child-report-not-collected` 无法交付。
记录 082 查明：**并非随即停止**，主方首次 collect 到失败终态后又调用模型 17 次、花费 562,727 token，
约 18.52 分钟后才失败。助手尚余 1,077,250 token 和 73 步额度，不是累计预算耗尽。

**65,759 不能解释为不可压缩的必需证据。** 原请求重建显示，占位符 19,766 token、永久豁免的读回
27,970 token，合计占约 72.6%；仅缩短占位符的离线估算可降到 54,478，尚未加入一次公共警示成本，
也未实际调用模型或证明质量。后续应先接及时停止、精简占位、实现有界读回保留与有限收尾。
扩大窗口可作为单独配置实验，但不替代上述机制；本次诊断没有改配置重跑。

### 6.6 后续修复已实施（见记录 084）

记录 082 列出的四项修复已全部完成并各自反向验证，后续真实运行又暴露并修掉第五、第六项；
实施、真实运行序列与验证边界见 [记录 084](084-fold-maturity-fixes.md)：

1. **及时停止**：判定移到每个 Step，终态失败或空交付在该次 collect 当步结束，
   不再等模型宣布完成。测试改为断言"终态 collect 之后 Provider 调用增量为零"——
   旧测试只断言抛异常，时机靠注释，属于空验证。
2. **紧凑占位符**：232 → 102 token（真实摘要下实测）。去掉 67 份重复的散文说明，
   去掉我自己造的 effect_id/digest 二次抄写，去掉对模型决策无用的协议元数据。
   按 run3 的 67 次折叠推算，19,766 → 约 6,834。
3. **有界读回保留**：撤销永久豁免，改为按 UTF-8 字节容量保护重开页，
   容量写进 fold 事件以保证重放可重算；老化出去的重开页折回**原始页地址**
   （取自该次读回调用自身参数），幂等一层，不再层层套壳。
4. **有限收尾**：从已授予额度中预留一段，到线后 Step 视图**清空工具列表**并附收尾指引；
   主方保留 collect 工具直到报告齐备。助手因此首次真正交付。
5. **需求加权的读回保留**：run7 显示抖动从"重跑原工具"搬到"重开同一页"
   （60 次读回中 31 次完全重复、五页各被重开 4 次）。改为在同一字节预算内
   按已证实需求排序、新近度只作次序后，run8 的重开多重度上限降到 2。
6. **放宽单次请求的传输重试**（仅试次脚本与预登记，不改发布默认值）。

**上下文越限自 run4 起未再出现**；run7–run9 终止于供应商传输中断，不是机制结果；
固定 Verifier 在整个序列中一次也没有跑到。

### 6.7 执行条件（备查）

条件已冻结在
[c4-preregistration.json](../validation-data/real-repository-pilot-v1/c4-preregistration.json)，
执行脚本为
[`tests/real_repository_evaluation/c4_context_trial.py`](../../tests/real_repository_evaluation/c4_context_trial.py)，
它走原 `traceh eval` CLI，不另建 Runner。

材料从 v10 冻结归档 `.traceh/f10a/artifacts/materials.zip` 原样解出到 `.traceh/c4-material`，
`dataset.json` 未改动且声明摘要仍然校验通过；原 v10 目录保持只读。

**已披露的条件变更（因此这不是对 v10 的受控对照）**：

1. 两个角色的 `max_output_tokens` 提到 32768（v10 主方 16384、助手 8192）。
   这是降低截断风险的探索性条件变化；64 token 探测不能证明原配置每次都会空交付。
2. 两个角色写入显式 `context_policy`（窗口 96000、输出预留 32768、余量 2048、
   触发 60%、回落 35%、保护最近 2 组）。窗口是**宿主承载决定**而非对模型能力的断言：
   输入硬上限 61,184 低于 v10 已经成功发送过的 67,984，所以必定在该模型已接受的范围内。
3. 两个角色授予三个读回工具，否则折叠等于销毁证据而不是延后取用。

离线已核验：材料经真实 benchmark loader 加载通过，两个角色都拿到
`input_limit=61184 / trigger=36710 / relief=21414 / step_fold=True`，
`max_output_tokens ≤ output_reserve_tokens`；Docker 29.2.1 与冻结镜像
`traceh-rr-eval:dev-20260920` 均在位；读数脚本已用 v10 冻结证据干跑验证（见 §5.1 的表）。

停止条件（预登记，不得改参数重跑）：安全合同失败、无法解释的未知用量、Provider failure、
同一精确页"折叠→读回并消费→再折叠"三次的抖动、以及超出自身冻结的墙钟与 token 预算。

## 7. 已知边界与未做的事

- 本轮**没有**实现语义摘要扩展、有界收尾续写、自动提高输出上限，也没有扩大实验批次。
- 本轮**没有**改动 Supervisor 为跨进程服务，没有改评分规则或固定测试。
- 基线失败（改动前即存在，已在干净 HEAD 独立复现）：
  `test_candidate_comparison.py` 的两个 probe 用例、
  `test_evaluation_workers.py::test_deliberately_regressed_candidate_uses_real_requests_and_original_memory`、
  `test_active_retrieval_grid.py` 的两个用例。它们不是本轮引入，也未被本轮修复。
- `src/traceh/llm/scripted.py` 的 UP037 是改动前既有的 Ruff 提示，未顺手改动无关代码。
