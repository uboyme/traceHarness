# Stop C 修补 C2 验证记录

状态：C2 修订后的真实网格及 Skill 回归已达原门槛，SQLite 独立核算、主线接入和定向门禁均完成。C3–C5 尚未完成；
不表示 Release Stop C 或 v0.9 发布通过。顺序见 [执行计划](plan/TRACEHARNESS_V0.9_RELEASE_STOP_C_EXECUTION.md)。

## 1. 冻结范围

使用 [显式语料](../tests/live_reference_journeys/corpus.json) 和原 Skill 导航语料，14 条核心旅程
各重复两次，每模型至少 26/28 有证据支持的任务完成；scope/replay/invariant 违规必须为 0。
另两条语义改写各重复两次，作为 C3 前词法诊断，不混入核心分母，也不能将失败算成语义成功。
模型为原显式网格 qwen-plus、qwen3.8-max-0902、deepseek-v4-pro-0813，各自单列。

首次 C2 调用前冻结 `c2-r1-code` 和 `c2-r1-frozen.json`，260 个文件有内容摘要，生产 `.py` 与
C1 最终验收源码逐文件一致。冻结前为 Memory+History、三来源题补齐明确的 unknown freshness
要求；题目、答案、源要求和原阈值未改。温度、步骤、输出、超时、重试及每题实际 Context 策略
进入候选 manifest；宿主 Memory/Project/Skill 限制在前置调用前落入 fixture-policy。

## 2. 实际主线与判定

- Memory 通过原 ProjectScope 与 declare/approve/revoke/supersede 准备，跨会话与跨项目确实创建并
  绑定不同 Session；所有长期事实仍来自同一 SQLite 的项目 Memory 事件。
- History 前置回合也走真实 Provider。压缩保留原来源事件，摘要不含答案；旧文件题先有成功的
  `read_file` 和非空 Git revision，再修改并提交隔离夹具文件，验证相同来源的 revision 确实变化。
- 答案必须为冻结键与字符串值的 JSON 对象；不修补、强制转型或模型自评。目录、回执、曾经读过
  或正确猜测都不足以证明正文证据：要求最终 composed/dispatch 的当前参考中仍有正确来源与正文。
  文件题要求成功 Tool 结果及对应的实际 Tool message，不能把搜索词回显当成文件内容。
- 原流、SQLite、所有尝试和失败都保留。原 Session reader、invariant checker 和请求重建验证全部
  夹具 Session，包括前置、外项目与新会话；前置调用与目标回答用量分别统计，缺少 usage 不补零。
- [运行脚本说明](../tests/live_reference_journeys/README.md) 是显式开发验证，不新增产品 Runner、
  模型 Tool、权限、可变事实源或生产默认值。诊断子集不能宣称完整验收。

## 3. 定向预检

`tests/test_live_reference_journeys.py` 的 22 项主预检通过，194.91 秒；另 2 项 manifest 和
Attempt/usage 检查通过，0.67 秒。均为离线替身检查评估器，不能计为真实模型任务完成。
相邻 `test_reference_retention.py`、`test_history_freshness.py`、`test_memory_context.py` 共
28 项通过，104.79 秒。它们检查既有保留、版本观察与 Memory 来源边界，不属于全量。
覆盖 14 条 core 前置和原 Runtime/SQLite/Git、普通文件读取、连续历史页、当前与旧文件、混合来源，
以及“猜对但无证据”、错误项目、真实文件读取失败、严格 JSON 拒绝和子集不能冒充完整网格。

预检原失败保留：重复使用夹具 Tool ID 触发 single-tool-result；两条混合题遗漏 freshness；
脚本对 policy 序列化形状的错误假设；测试选择了非目标章节及非法改写 Context 的无效判定反例。
分别修正夹具／评估脚本，生产保护未放宽。修正后上述定向集合通过；未将早期失败当作生产 Finding。

compileall 与修改范围 Ruff 通过；collect-only 收集 3283 项，仅收集，没有执行全量。
两版上下文 7.10 与执行计划同步当前状态；文档、差异门禁继续随最终结果确认。

## 4. 当前结果与边界

完整基线及摘要见 [机器记录](validation-data/stop-c-c2-baseline-2026-09-08.json)。

| 模型 | core 任务／严格答案 | 最终正文证据 | 语义诊断任务 | core 门槛 |
|---|---:|---:|---:|---|
| qwen-plus | 11/28 | 21/28 | 3/4 | 未达 26/28 |
| qwen3.8-max-0902 | 15/28 | 28/28 | 3/4 | 未达 26/28 |
| deepseek-v4-pro-0813 | 27/28 | 28/28 | 4/4 | 达到 |

所有 scope/replay/invariant/evidence-reader 违规为 0。96 条真实旅程共 234 次 Attempt，233 次
exact usage 合计 813,414 tokens：前置 42 次、107,517 tokens；目标 192 次，其中 191 次 exact、
705,897 tokens，另一次失败缺少 usage，未补零。语义诊断仍运行原词法策略，不表示向量能力。

最终 `audit.py` 重新打开全部原 SQLite，比对 exports、实际 Context 策略与真实用户问题，核验
全部 Session 与请求，并直接从原 assistant/message 和 turn/end 取得最终回答及结果再评分。
报告不是答案事实源。复制一份真实成功夹具，同时篡改报告及 result 的 final_text，会按
`Final response differs from SQLite` 被拒绝；原证据未改。增强答案绑定后完整核算再次通过，
上述分数保持不变。

失败包含有正确值却附加解释而违反纯 JSON，以及把未展开的 Memory／History 目录或下一页当成
证据不存在。仅扩写共享提示的七例真实探索为 2/7；改为末尾完成提示的七例探索为 4/7、正文 6/7，
仍不足以证明修复。上述 format 1 探索的题目、答案和门槛未调整，当时生产源码保持 C1 最终版本。

另外 40 次原 Provider 单请求诊断只检查格式或下一次披露决策，没有执行 Tool，不计为 Runtime
旅程或验收。原三份失败请求各重复两次：原布局 0/6、扩写提示 4/6、原任务重述 1/6、两者结合 3/6。
另一组两份请求各重复两次：原布局 1/4、可读动作提示 2/4、末尾完成约束 4/4、两者结合 4/4。
这些样本很小，且后续完整七例仍失败，所以没有据此改动 Context 协议或宣称模型行为已稳定。
增加 Memory／History 可读动作的隔离七例探索为 5/7、5/7、7/7，scope/replay/invariant 违规为 0。
动作来自当前已有 ID、版本及游标，经同一 renderer 计入预算，不新增 Tool 或权限；离线公开
Runtime 检查覆盖 Memory 读取、连续 History 页及 Tool 被拒绝时正文仍不披露。这一候选当时仅在隔离副本，
这些七例探索不足以宣称 C2 验收，后续完整比较见下文。

## 5. 评测输入歧义修正与新冻结

核查上述原始回答发现：format 1 只要求返回字符串，没有明确数字不得带单位，以及文件状态应取
代号而非整行文字。例如两项候选回答已给出正确事实及正文证据，但因附带单位或原行前缀被判错。
这是评测输入合同不完整，不能据此修改生产答案内容。对象外解释、未读取正文等失败仍有效。

format 2 增加显式 `answer_fields`，模型可见每字段的单位、代号形式要求，不含预期值；缺少描述
或字段不一致在模型调用前拒绝。原事实、query、预期答案、源要求、权限、调用边界及 26/28 门槛
逐项比对保持不变。实际用户问题因此改变，新旧结果不能混合比较或追溯改分。

原 format 1 全部结果保留。format 2 用原实现、阅读动作候选、精简视图分别冻结完整网格，每组
三模型各 32 条旅程。结果均经原 SQLite、真实问题、策略、原 assistant/turn 与请求重建独立核算。

| format 2 方案 | qwen-plus 核心 | qwen-max 核心 | DeepSeek 核心 | 全模型门槛 |
|---|---:|---:|---:|---|
| C1 原实现 | 20/28 | 27/28 | 28/28 | 未达 |
| 增加阅读动作 | 22/28 | 28/28 | 28/28 | 未达 |
| 精简视图并区分导航／正文 | 27/28 | 28/28 | 28/28 | 达到 |

原实现和动作候选共 192 条旅程、493 次 exact Attempt、1,782,573 tokens；最终精简视图为 96 条
旅程、250 次 exact Attempt、840,126 tokens。最后正文均 28/28 齐全，scope/replay/invariant/
evidence-reader 违规为 0。三模型各 4 条语义诊断都通过，但仍运行词法检索，不能称向量验收。
完整摘要、用量和摘要绑定见 [format 2 机器记录](validation-data/stop-c-c2-format2-2026-09-08.json)。

另外两组单请求诊断不计 Runtime 验收：任务前置 18 次中原布局 1/6、重复任务 0/6、移动任务 2/6；
精简对照 24 次中原布局 1/6、只区分导航／正文 2/6、只精简 2/6、两者结合 4/6。结合方案的四次
历史读取决策正确，两个纯 JSON 回答仍失败；仅用于选择后续完整候选，没有替代上述网格。

字段合同的正向、缺失／歧义拒绝，以及预期值变化不改变问题的 3 项检查通过；评估器全文件 27 项
通过，261.58 秒。实际导入旧冻结问题生成器后，这 3 项均按漏描述／未拒绝的原因失败，原逻辑已恢复。

## 6. 最终接入与相邻回归

已按 [ADR-0050](adr/0050-compact-reference-navigation-and-model-view.md) 接入精简视图与共享完成提示。
Session 9 / Context 8 / context-json-v8 / policy v5 是唯一协议；Session 8 / Context 7 保留为隔离
探索。原来源 block/provenance/收据仍完整保存在 Context；模型视图和 read_action 从其派生，原
Tool/Policy、资格、预算与保留链继续负责安全和生命周期，没有新缓存、权限或事实源。

相同源码另跑原 C1 完整 Skill 网格，严格与任务分别为 24/24、23/24、24/24，达到原至少 22/24 严格、
23/24 任务的门槛。最后正文均齐全，scope/replay/invariant 为 0。72 条旅程、183 次 exact Attempt，
575,886 tokens。独立核算直接比对 SQLite、原事件、最终 assistant/turn 和实际渲染请求。
核算器初次只因 Windows 路径分隔符与冻结清单不一致失败，按原 runner 的本机路径表示核对后通过；
未修改原运行证据或分数。

两处模型行为失败保留：C2 qwen-plus 一次附加解释而违反纯 JSON；Skill qwen-max 一次请求未披露
内容，两次被原工具拒绝后又调用 list_files，被原 Policy 拒绝。这些不计成功，不据此放宽参数或权限。

隔离 owner 集合 151 项先通过，9 项因旧模型展示字段／旧头部断言失败；同步成最后一条当前参考、
顶层导航字段和实际可见正文断言后，包含这 9 项的保留文件 10 项通过，共覆盖原集合 160 项。
阅读动作的公开 Runtime 正向、工具不可用拒绝、连续页和预算 4 项通过。预检中的未启用 reader、
把头部术语误当作实际动作对象两次断言错误也保留，分别修正测试输入与 JSON 内容断言，未改生产保护。
三个真实流程前置检查通过。格式化副本与冻结实测源码的 244 个模块 AST 相同；修复纯换行 Ruff 报告后
四个生产文件 Ruff 通过。接入后的当前 renderer 逐字节重现已核算的 180 个 Session、433 份 composed/
dispatch 参考请求，另有 244 模块源码摘要与 AST 等价绑定，未重新调用 Provider 冒充不同功能版本。

当前源码 owner 集合首轮 215 项通过、1 项旧预算夹具不再超限；精简后该 Memory 实际装得下，按原
覆盖规则压制较弱候选属于预期行为。将测试原文明确设为超过 Context item 限额后，先被夹具自己的
1024-byte Memory 限额拒绝；随后单独给该合成事实配置足够的批准容量，保持两个 owner 的限额独立。
生产限额和排除规则均未改。完整精度与共享预算两文件 31 项通过，32.44 秒；另一组相邻 153 项通过，
28.65 秒，覆盖 prompt/Composition、Inspector、原引用 Tool、Memory 失败、配置与架构保护。
所有范围内已知失败已清零；两次夹具失败保留，没有算作生产修复。compileall、修改范围 Ruff、
差异及文档检查通过；collect-only 3292 项，仅收集。生产文件未出现合成答案、模型名或本机路径默认值。
C2 已完成，后续 C3–C5 按执行计划继续。

未运行全量、L2–L4、Wheel/安装、发布级门禁；未修改仓库 Git 历史或推送发布。
