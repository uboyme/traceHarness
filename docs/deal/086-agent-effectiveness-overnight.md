# 086：三项目标的有界实验与逐轮根因记录

日期：2026-09-22。状态：进行中；首个 Single 失败已保留，尚无完整收益对照。

## 1. 授权与全局退出条件

用户明确授权目标模式，完成上下文治理、Single/Multi、有界分工策略优化实验及简历更新。
新增真实 API 总预算最多 **120 元人民币**，包含诊断、任务、策略分析和审阅；历史费用另列。
原 C4 的身份、账本、重放、未知用量、取消收敛、Provider 和冻结额度停止条件继续有效。
无 commit/push/tag/release 授权；既有付费证据只读。总目标和防局部循环规则见
[三项目标计划](../plan/TRACEHARNESS_AGENT_EFFECTIVENESS_PLAN.md)。

目标不是预设正收益。每项最终报告须包含完整成本、固定验收结果、全部失败和覆盖缺口。
公开 dev 内部验证不是官方 test，也不能称为模型从未见过。简历只能使用已实际得到的结论。

## 2. 先冻结选题，再准入

候选列表在任何新模型结果出现前冻结于
[授权与选择记录](../validation-data/agent-effectiveness-v1/overnight-authorization-and-selection.json)。
使用原固定 dataset revision 和 dev 队列，保留原三道开发题，取九道未参与调参的内部验证候选。
仅材料/环境不能满足既有合同可以按候补顺序补位，模型失败不能换题。

材料仍由 `tests/real_repository_evaluation/materials.py` 制作，准入仍由原
`preflight.py` 通过生产 Sandbox 执行原缺陷和参考修复；没有第二评测器。
参考答案留在宿主材料目录，不复制进 Agent 初始树。

| 观测与根因 | 最小处理与判据 | 当前证据 |
|---|---|---|
| 两份完整归档超过原 32 MiB 下载上限 | Git tree 显示完整树约 114/69 MB，仍在原 128 MiB 树额度内；只提高下载额度到 128 MiB，不删数据 | `admission-settings-large-archives.json` 与 `large-archive-selection.json`；原失败保留 |
| pvlib-1606 深目录写入失败 | 缩短本次输出路径重新制作；不改公共文件系统实现 | `.traceh/ael/`，随后发现下行独立阻断 |
| pvlib-1606 原题面 6409 字符，公共 Product 上限 4096 | 排除本轮材料，不截短题面、放宽产品合同或修改原题 | `reserve-selection-1.json` |
| astroid-1866 的上游 F2P ID 被截断为同一前缀，对应多个参数用例 | 当前一对一映射无法无歧义恢复；保留原失败/参考输出并排除，不猜一个 ID | `.traceh/aep1/pylint-dev__astroid-1866/` |
| marshmallow-1343 在 Python 3.10 访问 `collections.Mapping` 失败 | 使用 Python 3.9 的隔离镜像，保持原题与断言不变；原缺陷失败、参考通过才准入 | `.traceh/aep2/marshmallow-code__marshmallow-1343/admission.json` 已通过 |
| pvlib-1154/1072 缺 pandas 等环境依赖 | 独立镜像显式安装依赖，冻结镜像身份；不改源码和测试 | 两题 `.traceh/aep2/*/admission.json` 已通过 |
| 候补 sqlfluff-2419 完整树含链接 | 当前普通文件合同不支持，按既有规则排除 | `.traceh/aer1/acquisition-results.json` |
| 候补 pvlib-1854 的上游用例要求 `mocker` fixture | 现有冻结裁判关闭插件自动加载；单独判定显式插件环境支持，不计作 Agent 失败 | `.traceh/aep2/pvlib__pvlib-python-1854/` |

配置、选择与排除记录位于 `docs/validation-data/agent-effectiveness-v1/`。
九道新题均已通过原缺陷/参考修复准入，加原三道开发题共十二道。最终内部验证列表、材料、环境与
准入摘要见 [冻结记录](../validation-data/agent-effectiveness-v1/internal-validation-materials.json)。
pyvista 最终使用 bookworm 镜像，补齐 GL、VTK、tqdm、meshio 等原测试依赖；原断言未改。

## 3. 集成检查

完整 `python -m pytest -q --durations=30` 已结束（exit 1），原输出保存在
[集成日志](../validation-data/agent-effectiveness-v1/integration-20260922-0338.log)。
16 项失败与 17 项安装 ERROR 与交接清单逐项一致，没有清单外失败；不是绿色门禁。

只读检查发现源码版本为 0.11.0，当前解释器安装元数据为 0.9.0；Python Quality 和 Plugin Creator
两个示例 Distribution/Manifest 的上限仍为 `<0.9`。已在独立测试 venv 安装当前源码，未修改全局
解释器；两个示例的 Distribution/Manifest/自身测试上限同步为 `<0.12`，实际安装回归尚待运行。
另有 UTF-8 JSON 被测试辅助读取隐式 GBK 解码，已显式 UTF-8；引用预算测试未区分正式合同中的
reference quota 与独立 active request echo，已按原合同断言；Skill 夹具上限未覆盖新增披露元数据，
仅修正夹具额度，过大 Memory 仍按相同额度构造反例。对应八个测试文件定向回归 exit 0。
剩余旧 shell/Verifier 夹具未装配 Sandbox，以及 L2 可信 HEAD 验收仍待处理。不会放开宿主执行
或用当前工作区冒充可信 HEAD，不修改评分规则或删除测试取得绿色。

## 4. 付费开发试次与转向

首题沿用已暴露且已准入的 astroid-1196，先 single，再在相同条件下 multi。
旧 C4 的长延迟不能仅归因于系统：输出中推理占比较大，且当前 adapter 没有显式思考模式参数。
拟使用同一 Provider 的 `qwen3-coder-next` 作为新的冻结模型条件；先核实真实调用形状，
不把更换模型与配额后的结果宣称为相对 C4 的协作改进收益。
官方能力表和具体页面存在不一致描述，真实返回形状必须保留，不能假定支持或关闭了思考。

准备配置见 [development-settings-v1.json](../validation-data/agent-effectiveness-v1/development-settings-v1.json)：
整树 100 万 token，主方单 Turn 10 分钟，助手 25 万 token / 18 步 / 5 分钟，保留原收尾、
原文读回、身份、完成报告和固定 Verifier。其作用是产生可解释的有界结果，不保证题目成功。
源码摘要、确切配置与下一批费用预留必须在实际启动前重新冻结；准备摘要不冒充运行摘要。

官方价格核验来源：[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、
[Coder 使用说明](https://help.aliyun.com/zh/model-studio/qwen-coder)。费用估算不抵扣缓存或免费额度；
按实际输入档位计算，未知用量保守占位并停止继续付费。软件 token 上限不是供应商账单硬保证。

### 4.1 第一轮 Single：工具协议使用与重复调查

原证据 `.traceh/ae01/report.json`；冻结条件见 `dev-single-01-preregistration.json`。
10 分钟 Turn 到期后取消收敛，固定 Verifier 未执行，不能算功能失败或成功裁决。
50 次已知付费响应估算 0.911015 元；另一次取消请求没有返回 usage，按整轮 12 元保守预留。
该取消请求有冻结输入和有限 8192 输出 cap，属于原 C4 明确允许解释的未知用量，不能记为零。

实际 dispatch 中 `search_text` 声明必填 `query`，模型却反复传 `pattern`；错误回执在后续请求可见。
之后又反复读同一段 `Dict.getitem`。本次没有发生折叠，不能归咎于折叠导致忘记。
另有假设：每步最后的完整题面 user 引用包可能促使模型重启任务。用同一冻结请求做四次
原样/去掉末尾包的诊断，不执行返回的工具；原样一次推进一次重复，去掉后一次错误搜索一次新读取。
结果不足以支持改协议，费用 0.055260 元，原请求/响应在 `.traceh/aediag1/`。

### 4.2 第二轮：只改变模型

`qwen3.6-plus` 同题 Single 已结束并取消收敛，未进入固定 Verifier；本轮源码和任务配置保持不变。新冻结记录
`dev-single-02-preregistration.json`，预留 14 元；此时已知费用 0.966275 元、未知预留 12 元。
理由是先区分模型工具遵循能力，而不是在尚未证实的上下文假设上增加实现。
价格按官方北京区输入 2 / 输出 12 元每百万 token，当前输入上限低于 256k；
[官方模型页](https://help.aliyun.com/zh/model-studio/qwen3-6-plus)。这轮不是模式或压缩收益对照。


### 4.3 第二轮结果及交付预留根因

`.traceh/ae02` 完整耗时 623406 ms，24 步、23 次工具；23 个精确付费响应估算 0.575928 元，
另一个有冻结输入与 8192 输出上限的取消请求未知，保留本轮全部 14 元预留。
累计已知 1.542203 元、未知预留 26 元。没有把预留记作真实账单，也没有漏算失败。
原报告仍失败：已改出补丁，但持续自测直到 10 分钟截止，未正常交回；副本重放 26 份请求一致。
宿主只读离线诊断 `.traceh/ae02check2` 在保留工作区执行原固定裁判，26/26 通过，
不改原试次结果，也不把事后补验说成端到端成功。首次辅助脚本环境映射错误未启动命令，
失败目录 `.traceh/ae02check` 同样保留。

根因在 Product 装配层：Single 未绑定已配置的收尾预留；Multi/调查者的计时又按角色总时限，
没有以更短的 Turn 时限收紧。修复复用原 StepView 和事件投影，不改预算硬上限或引入新交付协议。
验证包含真实写入→撤工具→原固定 Verifier/Review/Promotion，以及 Provider 失败和撤回工具拒绝。
反向验证分别移除 Single 接线、移除 Turn 时限约束；结果另存日志，未完成前不宣称修复已验收。


### 4.4 修复验证与对照冻结

`delivery-reserve-directed-04.log`：六个真实 Product/Sandbox 用例全部通过；正常交付、Provider 失败、
撤回工具仍被调用后拒绝并交付，各覆盖步数和更短 Turn 墙钟两种触发。初版测试重复了 tool_call_id，
触发原 Session 不变量；随后纠正为一次拒绝后交付，并为最后交付保留实际可用步数，未改生产门禁。
`delivery-reserve-reverse-single.log` 和 `delivery-reserve-reverse-turn.log` 各一个反向用例在预期的
第二次请求仍有工具处失败；反向源码位于隔离副本，不修改正在被真实运行冻结的生产源码。
修复后首轮 `.traceh/ae03` 已在新源码摘要下启动，同题同模型同配置；这次不能沿用旧源摘要。

集成复跑出现一项新失败，保留目录的原比较报告证明原因为 `evaluation-frozen-input-drift`：
本 Agent 在完整检查运行期间修改了源码，两个子评估虽完成，冻结检查仍正确拒绝。
`test_background_optimization.py` 独立重跑 13 passed；这不替代源码稳定后的最终完整全量。

九道内部验证配对准备见 `validation-pairs-prepared.json`，各配对两臂原 CLI 独立 Worker 顺序执行，
首臂按预选序号交替，整树上限和原裁判相同。准备不等于已经执行或获得成绩。
上下文唯一候选将软折叠带从 60/35 改为 15/8，见 `context-experiment-preregistration.json`。
触发与缓解线是一组水位策略，硬输入、输出、整树预算、原文读回与裁判保持不变；三道开发题做
新的 Single 两臂任务，不将没有实际发生折叠的题说成上下文收益。因配置不同，使用原 Runner 的
两份独立完整报告作显式配置对照，不冒充要求同配置的原 text-candidate 比较。


### 4.5 修复后的首个完整 Single

`.traceh/ae03` 完整成功：Product/Workflow completed，固定 Verifier、Review、Promotion 通过，
工作区全部释放；24 份原请求重放通过，原数据库摘要不变。22 次真实响应均 exact，267176 输入 +
6661 输出 tokens，估算 0.614284 元；本轮无 Provider 失败或未知用量。累计已知 2.156487 元，
历史两次取消仍保留 26 元占位。Multi `.traceh/ae04` 已按同源/同模型/同预算/同裁判另行冻结启动。

`ae03-phase-timing.json` 的原事件区间显示：模型活动累计 139975 ms；9 次 shell 工具区间累计
298387 ms，准备请求区间累计约 90.18 秒。至少一次 Sandbox outcome 的容器 started/finished 只差
约 0.75 秒；工具区间还包括宿主沙箱准备、工作区处理与收敛，不能称为纯测试运行时间。
这是观测性区间分解，不是 CPU profiler 或普遍归因；本地同时有集成测试，墙钟需披露该负载条件。
不据此在实验中途改沙箱生命周期或悄悄移除隔离；后续可单独评估批量自测与宿主开销。


### 4.6 Multi 到达验收，但补丁未通过

`.traceh/ae04` 已正常交回，原固定 Verifier 失败，Review 未通过、没有 Promotion；所有资源收敛，
34 份原请求重放一致，原数据库未变。32 次付费 exact 响应（主 30、调查助手 2），402044 输入 +
10560 输出，估算 0.930808 元。原两个取消预留不变；截至本轮累计已知 3.087295 元、未知预留 26 元。

助手两次请求即完成，不能归因为助手不愿停。主方先查实现和测试，再让助手重复同一段源码；
还在计划阶段试图写入，并发计划最初把 children 传成字符串。其后正常纠正、派发、收回与交付。
宿主只读复核 `.traceh/ae04check1` 复现固定检查 1 failed / 25 passed：
`test_unpacking_in_dict_getitem_uninferable` 中不可推断输入原行为被破坏，出现 NameInferenceError。
这是当前补丁错误，不修评分、不回填原结果；也不把整个任务的成本差都归为助手自身。

原 AO 已以以上开发观察启动一个候选：唯一可编辑 ALLOCATION_GUIDANCE，原双臂各一次，
一次分析，原预算/材料/固定裁判/Review不变，绝不自动采用。若双臂都失败，即使成本门槛通过，
也不能在本轮人工结论中称为可采用改进或量化优化收益。候选若值得留出验证，必须先冻结。
同时启动三题上下文配置对照。独立批次最多两个并发、每对两臂仍串行且首臂顺序预定；共享宿主的
墙钟仅是该负载条件下的观测，质量与完整 tokens 为主要对照量。
总费用分配为历史已知 3.087295 + 未知预留 26 + AO 上限 29 + 上下文 21 + 九题配对 39 =
118.087295 元，尚有 1.912705 元未分配；批次内部计数从零开始，不重复漏算历史或给每批各花 120 元。

辅助脚本的一次广域 compileall 误扫取得的开源材料，产生新字节码缓存；清理被自动审批拒绝，未绕过。
实验副本早已复制，不受影响；`prepared-material-integrity.json` 记录原 evaluator 对全部 15 份
已冻结模式/上下文材料的再次核验。没有改原源码、准入分数或已付费运行。


### 4.7 原生有界优化：未知用量停止，不采用候选

第一轮分析 qwen3.6-plus 在 120 秒控制预算内未返回，被原控制面取消；有限 4096 输出上限已冻结，
保留 1 元未知费用额度，未产生候选和评估。第二轮只把无工具分析模型改为 qwen3-coder-next，
3285 输入 / 890 输出，估算 0.006845 元；生成并准入一个仅追加 ALLOCATION_GUIDANCE 的候选，
要求助手解决不同的未决问题或负向行为，避免重复主方已查证的内容。没有自动应用到主源码。

第二轮原生评估在模型派发前遇到 workspace-git-failed，零付费。原公开 Runner 离线复现证明
Windows 深层目录使上游文件 checkout 报 Filename too long；显式短输出目录可进入请求。
原失败报告保留；在 C:/Users/caojie/tae-ao3 用原 run_manual_optimization 评估已生成候选，
不重复花钱生成提案，也不使用替代评分器。证据 native-worker-path-diagnosis.json。

最终原生两臂都遇到 protocol / provider-tool-arguments-json-value-expected；baseline 19 次、
candidate 11 次付费尝试，各有一次失败用量未知。已知费用分别 0.478328 / 0.220152 元，
各保留 14 元未知额度；原 optimization report 明确 action=stop、reason=evaluation-usage-unknown，
两臂资源收敛、不变量通过，但没有可比完整成本或质量收益。原请求重建分别 21 / 13 份通过，
原数据库摘要不变。调用数下降不能冒充候选改进；未采用、未声称自进化收益。

这再次触及 084 §10 待批准的宿主分类决策。新方案只建议在原 Provider/Attempt 主线上分开
参数采样语法失败和坏响应信封，先证明记账与有限重试边界；未修改持久协议或重试策略。
见 [待批准决策](../plan/TRACEHARNESS_PROVIDER_SAMPLING_DECISION.md)。

### 4.8 第一组上下文对照：局部 token 改善没有变成完整交付收益

原题 marshmallow-1359：baseline aec1a 30 次真实调用、410909 tokens、529936 ms、0.887938 元；
提前折叠 aec1b 36 次、371597 tokens、631968 ms、0.838704 元。原事件中 fold 为 0 / 21，
估算峰值输入 21452 / 12843，越限 0 / 0；检索/读取调用 16 / 26。
因此总 tokens −9.57%、已知费用 −5.54%，同时调用 +20%、共享宿主墙钟 +19.25%。
这是单组观察，不能做显著性推断，也不能把所有墙钟差归因于策略。

两边 patch_sha256 完全一致，固定 Review 都失败；只读复核原保留工作区为 1 failed / 76 passed。
唯一失败 test_datetime_list_inner_format：嵌套 DateTime 本应继承 Schema 的 iso8601 设置，
模型把读取改成带默认值的 getattr，避免属性异常，却退回默认 iso，丢失原语义。这是补丁缺陷，
不是折叠损坏补丁的证据；宿主未替模型修改答案或回填原分数。原 aec1a/aec1b 重放 32/38 份通过。

原批处理第一轮在成功产出报告后，误读不存在的序列化 evidence.converged 而中止；修正实验
汇总脚本为原 trials 的 convergence/invariants 字段，原报告未改，也未重跑 aec1a。
AO 触发停止时只结束 run_context 调度父进程，已在运行的 aec1b 保持原预算正常收尾；
手工调用原只读汇总/重建完成记录。其余四个上下文运行及九题内部验证均未派发。

### 4.9 费用与集成状态

目前已知估算总费用 5.519262 元；未知额度为早前两次取消 26 元、分析取消 1 元、
原生 AO 两臂各 14 元，共 55 元。已知加保守预留为 60.519262 元，低于 120 元；
这不是供应商账单，也不能把余额作为绕过停止条件的理由。原未知预留没有因任务失败被清零。

集成复跑还暴露两个测试边界：干净 L2 环境缺少 tiktoken，取消测试若准备提前失败会无限等待
Provider 进入；显式补齐测试依赖并让测试等候原任务结束或进入信号，定向两例通过。
另外真实 Docker 才会执行的旧夹具仍使用 child 单值、readonly work format 2，以及假定 Catalog
只有助手 Artifact；改为现行 children / format 3 / 逐 assignment 回执，并分别验证助手与主方捕获。
结构化 Product、交付 Review、主动检索与 UE4 相邻用例通过；并发可写用例单独通过。
最终完整集成仍在推进，不能把上述定向结果称为全量通过。


集成约 64% 的新增失败是预算协商 Product 夹具：原事件证明 120 秒助手寿命超出同步交接
55 秒 Tool hold，CollaborationPlanInputInvalid 在派发前拒绝，child_steps=0。
将显式测试寿命/Turn 改为 30 秒、断言迁到 children 列表后，原测试和 investigation_budget
相邻共 17 项通过。保留 before.log 真实失败；仍要求助手两次执行、预算请求未批准、
主方及时失败，未放宽生产协议。最初定向命令引用不存在的测试文件，退出 4，单独保留日志，
不算验证；修正文件名后的 after2.log 才是通过证据。


集成约 67% 的两个 Product F3 失败已独立复现：SQLite 测试外层 30 秒 watchdog
在正常固定验收刚启动时取消，run_chat 返回 130；原事件有完整请求和正常主方完成记录，
不是启动前失败。改为使用夹具声明的整任务 max_wall_milliseconds，生产预算不动；
修复后真实路径 39.49 秒达到等待批准。
另一项审批展示测试误认所有角色输出上限均为 4096；当前调查助手显式为 1024。
改为逐请求按所属角色严格检查，并允许当前计划阶段的只读工具和 submit_collaboration_plan；
两个用例均通过（product-f3-before.log / product-f3-after.log），仍断言 Review/审批资料不进入模型。


后续本机 HTTP 离线探针进入公开 Provider.complete（不调用外部 API）：合法响应保留 7/3 usage；
合法信封及 usage 配畸形工具参数时，先抛出 protocol，usage 丢失；非法 usage 不会被记成 exact。
另一个 finish_reason=length 的截断参数得到相同 failure_code。因此线上两次仅能确认参数解析失败，
无法据当前日志区分语法采样问题和输出截断，不能据此盲目加重试。
待批准方案补充：先保留合法结束分类和用量，不完整响应按既有 C0/C1 处理，仅正常结束的明确
参数语法失败考虑一次有界重采样。所有证明仅为离线夹具，不补写线上未知账单或结束原因。


集成约 77% 的三个 retained-output 失败也已复现：旧测试要求读回页没有 output_ref，
而 080-C2 format 2 已规定每条完整结果都可寻址。修正测试为页面必须 inline 完整呈现，
再经原 resolver 还原同一内容，保留重启、分页、来源拒绝、取消及原命令执行一次的断言。
未改生产 retention/folding 协议。retained-output 与 output-reference-eligibility 合计 44 项通过；
证据 retained-output-before.log / retained-output-after.log。

### 4.10 shell 延迟的离线分解与读取长度根因

只读原 ae03 事件得出：shell 区间总 298.39 秒，容器实际运行 10.50 秒，准入至
sandbox/request 110.67 秒，outcome 至 publication 117.73 秒。后两段分别包含工作区快照/CAS
准备和发布前快照核对，不等同模型或多 Agent 协调耗时。Docker 只读 version/info/image 查询
分别 0.214/0.511/0.212 秒，只能描述当前共享宿主，不是隔离性能基准。

ae03 工作区已按原生命周期释放，直接 profile 因目录不存在退出，未重建或改写该运行。
改为只读 ae02 原 quarantined 工作区，在它的原显式 SandboxPolicy 下测到 375 文件、73 目录、
3203799 字节的 snapshot 15.02 秒，其中 BufferedReader.read 占 14.58 秒。
当前代码将每个文件的 read 长度设成剩余 workspace_bytes（本实验 128 MiB），即使文件很小。
同一组文件内容逐字节核对的对照：已知文件大小加 1 为 0.062 秒，随后按原剩余额度读取为
17.31 秒。没有读取受保护 .env，没有调用模型或执行新容器，没有改动原证据。

证据：sandbox-phase-timing.json、docker-readonly-latency-probe.json、snapshot-readonly-profile.json。
七条公开 snapshot 定向用例先运行旧逻辑，三个小文件分配边界失败，文件变化/读失败/总额度
四项通过（snapshot-read-bound-before.log）。完整集成结束后修正原 snapshot 单次 read 长度，
保留原 stat/身份/总额度/发布前完整复核，不增加缓存或新事实源。29 项快照/权限/发布回归通过，
独立进程内恢复旧读取表达式后，三条公开分配边界反例再次因预期 MemoryError 失败，仓库未反改。
修正后同工作区 snapshot 一次实测 0.256 秒；同次小读取 0.056 秒，旧大读取 10.69 秒，字节相等。
证据 after/reverse 日志与 snapshot-readonly-profile-after.json；不把局部测量换算成任务提速。

0445 全量最终为 4345 passed / 26 failed / 8 skipped / 0 error。完整结果与最慢 30 项见原日志，
机器汇总 integration-0445-result.json。旧共享 StructuredProvider 还影响六项 Verification Review；
末尾四项 writable completion 使用已移除的单值 integration 断言，改为唯一列表项再严格核对原
tool_call_id。该轮运行期间曾改生产源码，最终门禁必须在新稳定源码上完整重跑。
另外 ScriptedLlmProvider 的冗余字符串返回注解触发 Ruff UP037；干净 HEAD 同样复现，已做无行为
变化的注解修正。当前源码摘要为 2c24b47952e48b7a0a15a85d8b3c2837e3d2b857260a13fb61350786ffa5a428；
所有付费原运行仍绑定原摘要，不改写为这个版本。

集成后段另有四条旧 tool-fold 夹具失败，独立复现为 inline 被错误假定不可折叠、
占位符仍期待完整 output_ref、伪造测试还修改已移除的 kept_recent_turns。按现行 format 2、
可执行 read 动作及 boundary.kept_recent 修正，保留来源拒绝、原工具一次执行、重启读回、
取消/竞争/精确重放断言。tool_result_folding 与 in_turn_step_folding 合计 31 项通过
（tool-fold-before.log / tool-fold-after.log）；未改变生产折叠语义。

最终相邻回归 Verification Review、writable completion 与真实 Sandbox integration 共 21 项通过，
包括真实固定验收、修复后再验收、原错误结果的拒绝和重复取消收敛（final-adjacent-sandbox.log）。
编译、113 个改动 Python 文件 Ruff、4392 项收集、diff 检查通过。稳定源码完整复跑现在启动；
此后冻结源码与测试直到结果返回，不能把运行中的检查称作已通过。

### 4.11 L2 当前工作树夹具的冻结字节边界

0730 冻结集成中的 L2 已返回失败：核心回归 3825 passed / 70 failed / 316 skipped /
10 errors；安装、构建、doctor、候选测试通过，外层全量仍在继续。完整错误保存在
l2-0730-core-regression.txt / l2-0730-report.json，不能把嵌套结果与外层计数相加。

根因定位到测试拥有的当前工作树快照：其根目录白名单只有 Markdown/TOML，遗漏了
.gitattributes。该快照助手由本轮为检验未提交源码而新增，这是本轮引入的测试夹具缺陷，
不是改动前的基线问题，也不是生产摘要校验的缺陷。随后 Windows Git 克隆按环境换行策略重写冻结数据，公开输入读取器正确拒绝
evaluation-frozen-input-drift。这不是应该放宽摘要校验的生产问题。用真实
retrieval_episodes_v1 数据做两个独立本地 Git 克隆：省略属性文件时，JSON 语义相同但字节
摘要改变并被公开读取器拒绝；保留属性文件时字节一致且读取通过。证据
l2-clone-bytes-probe.json。最初选用本来已经为 CRLF 的 retrieval_v1 数据，负向对照没有触发；
该失败探针保留，不能将它作为有效证明，改用实际受影响的 LF 数据后反例才成立。

另外一个干净环境失败来自 request_token_meter 的真实运行/展示测试：显式安装了 tiktoken，
但该测试随后通过 presentation 导入 Rich，测试依赖未包含 Rich。计划在本轮全量结束后修正
夹具的根目录属性文件与显式测试依赖；源码和测试在运行期间保持冻结。不能以跳过此测试、
取消冻结摘要检查或降低真实门禁的方式取得绿色结果。

0730 完整集成已于本地 09:52 观察到结束：4392 收集，4383 passed / 1 failed / 8 skipped /
0 error，退出 1。计数来自完整外层进度字符和末尾失败清单，不把内嵌 L2 计数累加进去；
原日志与 integration-0730-result.json 保存最终证据。唯一失败是上述 L2 门禁。
源码摘要与启动记录一致；中途输入摘要记录时间为 01:34 UTC（本地 09:34），其源码/测试/输入
文件摘要与结束时完全一致。

全量结束后修正测试快照复制规则，使根 .gitattributes 随显式可信 HEAD 保留；同时补齐
Rich 测试依赖。新增小型真实 Git 克隆反例通过公开 referenced_input 检查字节身份。
candidate_validation 与 request_token_meter 的快速回归 39 passed / 1 skipped / 1 deselected；
被排除的是将由最终全量覆盖的真实 L2，跳过来自未显式配置 Docker 的 token 折叠相邻用例，
补上原显式 Sandbox 配置后该用例 1 passed。没有用跳过替代真实路径。
独立进程只移除该复制规则，同一个正式测试再次因公开 evaluation-frozen-input-drift 失败；
仓库代码未反改（l2-fixture-bytes-reverse.log/json）。本次修正没有改生产摘要读取器或运行时依赖。

修复后编译、117 个改动 Python 文件 Ruff、4393 项收集、diff 检查通过；八份受影响文档的
986 个显式相对链接、围栏、秘密模式扫描与 L2 两版主题对应检查通过。正式版 19.11/19.11.1
及通俗版 19.10.2 已同步属性文件与显式测试依赖边界。新完整检查包含原真实 L2，不另重复
同范围嵌套全量；执行期间重新冻结源码与测试，结果未返回前仍不称最终绿色。

0959 启动因 PowerShell 将 --junitxml 表达式拆成位置参数而退出 4，未执行测试。
对应日志、起止摘要与 completion.json 保留；修正为参数数组后使用新的唯一输出前缀启动，
不覆盖此次启动失败，也不将它记为一轮通过或源码回归失败。

### 4.12 全量剩余 TUI 失败：首屏布局与点击坐标竞争

100021 全量于 2026-09-22 12:24 本地时间结束：4393 项中 4384 passed、1 failed、8 skipped、
0 errors，耗时 8628.096 秒；真实 L2 已通过。唯一失败为治理右键复制测试等待菜单超时。
单独模块曾 10 项通过，不足以证明已修复。

原测试经独立 tmp_path 参数化运行 20 次，3 次重现同一超时。点击记录显示三次均在
RichLog 的 Region(y=31,height=0) 上计算坐标，实际派发时布局已变为 Region(y=28,height=3)，
Pilot.click 返回 False，菜单未打开；其余 17 次成功。原因是 pilot.pause 的暂时空闲不保证
call_after_refresh 中的初始正文及其 auto-height 布局已完成，不是菜单处理耗时超过 10 秒。
证据为 ../validation-data/agent-effectiveness-v1/tui-copy-race-before.json。

只修改测试：观察原 _render_initial_conversation 完成后的下一次刷新信号，再计算坐标，
并断言正文与治理页右键都实际命中。没有修改生产功能、持久协议或超时。修复后 20/20 通过；
独立进程移除等待、恢复 pilot.pause 后 2/20 在命中断言失败，同样记录高度 0 到 3 的移动。
证据为 tui-copy-race-probe.json、tui-copy-race-reverse.json、tui-copy-reverse.log。
最初重复复用 pytest Item 的诊断脚本产生 tmp_path KeyError，属于无效夹具探针，未计入上述反例；
有效探针改为独立参数化收集，实际进入原鼠标与菜单路径。

相邻交互回归 55 passed，涵盖选区/空选区/取消/退出/治理及布局；两个真实 Docker Product
执行用例未在本次纯测试同步修正中重跑。编译、修改测试 Ruff、全量收集、diff 检查通过。
正式/通俗版 13.11 同步测试同步边界。本次没有再启动长全量，最终集成绿色仍待集中确认；
不把定向通过追认成上次全量通过，也未恢复付费实验。

### 4.13 主线纠正为 Single：真实候选及首次对照

用户再次明确 Single 是主力，有界自进化也必须优化 Single。原 Multi 分工候选不能代替该目标。
从原 Product 角色消息原样提取 CODER_GUIDANCE 并加入原 UE-3 字符串白名单；不改变原默认指令，
不增加优化器、权限、状态或评分路径。127 项定向/相邻检查通过，含 Single/Multi 两种原生 AO
双进程、真实 Git/Sandbox、固定 Verifier、独立审阅及不自动采用；编译、Ruff、收集通过。
正式/通俗版 12.7 已同步。生产源码冻结为 b14349b43ea4f3df167670ca6fbc8ea448924ff8a8cedb073751215180bb4b0c。

新授权下恢复一个明确冻结的开发批次（single-ao-preregistration-v1.json），使用原每请求一次
Attempt，不新增采样重试。驱动进程清除代理变量并安装 ProxyHandler({})；原双臂 worker 也强制
direct，系统代理不变。输出位于操作员显式短路径 C:/Users/caojie/tae-s1，非通用默认。

真实分析 1584 输入/189 输出，费用估算 0.00234 元；候选只在原指导中补充相邻/嵌套行为验证，
无开发题身份或答案。原版 Single 36 次调用、已知 451196 tokens/1.023122 元；候选 38 次、
已知 547978 tokens/1.190996 元。两臂均到期取消，没有固定验收裁决，各有一次取消用量未知；
原优化器 evaluation-usage-unknown 停止，未采用，不能把已知 token 子计当总成本收益。
分别 38/40 份请求快照重放通过，两臂不变量和收敛通过，原数据库未变。

新根因：两臂反复向 argv 执行工具发送管道/重定向，产生参数错误；候选随后执行过宽的 verbose
测试选择，产生约 68KB 输出，工具已被收尾视图撤下后仍试图读回。第一版只加强检查范围，没有
约束验证成本与交付，不再围绕这一提示无界重试。第二个也是本批最后一个开发候选，观察包明确
包含此次失败，限定优化命令合同、最小必要验证及文本收尾。仍一次分析、双臂各一次，预留 29 元；
上一轮费用全部加入账本，历史未知 55 加新未知 28 共 83 元，已知 7.735720 元，连同新预留共
119.735720 元，小于总上限 120。第二批未结束前不派发上下文或验证集，结果以原输出为准。


### 4.14 冻结 Single 候选进入独立验证

第二个 Single 开发候选已结束：原版 305510 tokens / 0.684260 元，候选 285381 tokens /
0.640342 元；均进入固定验收且失败，未知为零。原优化器给出 development-candidate-qualified，
只说明当前二值质量未下降且成本门槛成立，不能据此声称正确交付、采用或泛化收益。
两臂分别 30/28 份请求快照独立重放，原数据库不变。第二候选冻结，不再生成第三个开发候选。

上下文 astroid 新配对两臂分别 201231/192298 tokens，调用都是 20 次，折叠 0/11 次；
固定验收均失败，收敛及不变量通过，无 Provider 失败或未知。原 Review 和重放结果保留在
context-astroid-v2；不是“未停止”失败，不能把约 4.4% token 降幅写成正确性保持的收益。
补丁不同，不能套用之前 marshmallow 的同补丁结论；需以各自验收记录诊断语义遗漏。

两次近期取消的未知计费按官方最大上下文、最高单价和实际输出 cap 给出保守上界，
各从整试次 14 元占位改为 9 元，原预登记未改、未知未清零，依据 single-ao-cancel-cost-bound.json。
当前已知 9.961589 元、历史未知占位 73 元；首题内部验证另预留 28 元，总计 110.961589 元。
原内部验证队列第一题 pvlib-1154 使用原生 text_candidate 双臂入口执行，候选逐字冻结自 tae-s2，
不再从该题生成候选；直连、不并发重型回归、不自动采用。此时验证尚未结束。


### 4.15 Single 独立验证与二进制归档核验修复

pvlib-1154 两臂原 Product/Verifier 均成功，baseline 147323 tokens / 16 次 / 0.394846 元，
candidate 166048 tokens / 17 次 / 0.407566 元，工作时间 318.581→288.449 秒。
候选 token +12.7%、费用 +3.2%，单次工作时间 -9.5% 不满足冻结成本门槛；不采用、不再从该题调参。

原离线 comparison 在两个 worker 成功关闭后错误返回 evaluation-manifest-invalid。
公开 inspect_experiment 触发到 comparison._load：对 28148442 字节 materials.zip 使用
read_input，误套 4 MiB JSON 文档上限；evidence.load_run 也存在同一混用。
修复归属 evaluation/inputs：新增同一 confined_path 校验下的流式二进制摘要，两个归档核验入口
复用；JSON 输入仍保留原限额，归档摘要、缺失/越界/链接拒绝未放宽，无协议或评分变更。

19 项定向/比较测试通过；64 项证据、审阅、合同、架构相邻回归通过（两组有重叠，不相加）。
公开比较测试先执行完整原双进程试次，再缩小文档额度至所有 JSON 之上、实际 ZIP 之下，
验证二进制合法而篡改归档仍被拒；另验证真实超过 4 MiB 的文件可核摘要、不可作 JSON 输入。
进程内恢复两个 owner 的旧读取逻辑，新公开比较测试在 complete 断言因原错误失败；源码未回退。
编译、修改范围 Ruff、全量收集、diff 检查通过，最终全量未重跑。

原 eval --compare 离线生成 single-validation-v1/comparison-rechecked，complete=true、
hard_constraints=passed、status=regressed；两臂 18/19 份请求重放，原数据库不变。
保留原 not_comparable 报告，没有模型重跑或追加 API。累计已知 10.764001 元、未知占位 73 元。

上下文 astroid 双臂只读复核均 25/26：test_unpacking_in_dict_getitem_uninferable 因
NameInferenceError 未转成预期异常而失败；两份补丁都把 infer 放在异常捕获之外。
这是补丁语义遗漏，不是取消/收尾未收敛；诊断目录 tae-c3a-diagnostic / tae-c3b-diagnostic
不改变原成绩。三题上下文计划只剩 pydicom，原 v2 准备未调用模型；归档修复后另以 v3
新源码摘要冻结，原先序 B→A 与 60/35 对 15/8 不变。预留 28 元，共 111.764001 元，
启动该最后配对；不再更换水位或生成第三个 Single 开发候选。


### 4.16 原定上下文三题收尾（2026-09-23）

pydicom B→A 原序完成：A 原版 101592 tokens / 12 次 / 0.223324 元 / 89.685 秒，
B 提前折叠 124445 tokens / 16 次 / 0.269440 元 / 129.098 秒；双臂验收通过、未知零，
请求快照 14/18 份独立重放通过。但两臂 surface/replace 均为零，不能把差异归因于折叠。
原定三题六试次完整保留；两道发生折叠的题未通过验收，不能声称已证明正确性交付节省。

新增累计已知估算 11.256765 元，未知仍预留 73 元，合计 84.256765 元；未与供应商账单对账。
无在途付费进程，无新试次预留。最小对照覆盖上下文、Single/Multi、Single 有界自进化，
但积极整任务收益、剩余八道内部验证与最终完整回归绿色未达成，不能宣布总目标完成。
简历只写已核验机制与观察，明确候选被拒绝，没有部署、主仓库提交、推送或发版。


### 4.17 根因驱动的新 Single 开发批次（2026-09-23）

用户再次要求继续，原 v2 独立验证负结果保留。只读对比 pvlib 双臂原事件：两臂固定验收均过，
v2 候选比原版多 2 次 read_file、多 1 次 shell，补丁与验证命令更大；
总 token 147323→166048。原 v1 开发轨迹还显示过宽 verbose 测试、到期取消。
因此 v2 的“无条件相邻/嵌套检查”在这类已正确验证的任务上带来额外成本，
但不是证明提示一项独自产生全部差异。新批次只尝试一个更聚焦的通用编码指导，保留合法 argv 与
宿主收尾要求；实际候选仍由原分析模型提出、白名单校验，原 Product/Verifier 比较。

pvlib 原为 v2 未见验证，结果已经用于 v3 根因分析，明确降为 v3 开发题，不能再充当 v3 独立验证。
若原优化器在 pvlib 开发对照合格且无停止条件，再用预冻结队列第二题 pydicom-901 验证冻结候选；
失败或未知超预算即停，不换题或继续生成第四版。独立验证题不参与 v3 提案。
原四臂条件、上限与先后规则在 single-ao-cycle-v3-plan.json 登记；本批开发最多一次分析、
一个候选、两臂，额外预留 29 元，既有 11.256765 元已知与 73 元未知占位保留，合计
113.256765 元低于 120 元。验证配对另需完整 28 元空间再启动。仍直连、每请求一次 Attempt，
候选不自动采用。此前“第二个为最后一个”限定旧批次；新批次由本次根因与继续执行授权触发。


### 4.18 新批次按退步停止

v3 分析一次：1872 input / 181 output，估算 0.002596 元。候选只改 CODER_GUIDANCE，
要求基于改动选择最小检查、argv 合法、宿主要求交回时停用工具；不含题目身份或答案。
原生 pvlib 对照：baseline 13 次 / 115621 tokens / 0.294702 元，candidate 13 次 /
129326 tokens / 0.309942 元；两臂均交付且固定验收失败，候选工具调用多 1 次。
原 comparison complete=true、hard_constraints=passed、status=regressed，30 份请求快照分臂
15/15 独立重放一致、原库不变，未有未知用量或 Provider 错误。优化器 max-rounds 停止，
没有派发下一道内部验证、没有采用候选、没有生成第四版。

累计已知费用 11.864005 元，未知占位 73 元，合计 84.864005 元。无付费进程、无新预留。
这轮显示原版自身也有单次波动：前一批 pvlib baseline 固定验收通过，此批失败；
不能挑有利一次作为策略因果结论。积极成本收益仍未证明。最终全量进入约 16% 的历史长 L2 段，用户质疑继续占用时间后立即中止；没有产生最终全量结果。保留 partial log，不作为通过或失败。


### 4.19 单/多扩大到原内部验证队列

用户指出三项实验样本不足。全量复跑中止于历史长 L2 段；partial log 没有全量结论。
不再生成 AO 候选或调上下文水位。本批优先回答原独立问题：相同冻结题目、Provider、
模型、工具装配、固定验收与整树上限下 Single/Multi 的质量和整任务成本。

原九题队列首题 pvlib-1154 已用于 AO v3 开发，不再计未见验证；保持原次序选择剩余八题
（pydicom-901、astroid-1978、pyvista-4315、marshmallow-1343、pydicom-1256、
pvlib-1072、pydicom-1139、astroid-1333）。16 试次是计划分母，不预称已执行。
每题沿原 pair.json 的双臂顺序，显式重绑当前源码摘要 feb4d0c8；离线 verify_inputs
和 plan 加载逐题通过，原材料/计划字节不改。当前已知 11.864005 元、未知占位 73 元，
单题两臂另预留 28 元，最高 112.864005 元。逐题结算后再判断下一题能否启动；
直连、无代理，每请求一次 Attempt，保留所有失败与缺臂，不以结果换题。
本批原始记录为 mode-validation-v2 和 mode-validation-v2-execution.log；其后停机经过见 §4.20。


### 4.20 扩样首题协议失败与停止时机

原队列第二题 pydicom-901 按冻结顺序先运行 Multi。原事件最后一次 model/attempt-end
为 status=failed、failure_category=protocol、failure_code=provider-tool-arguments-json-value-expected，
用量缺失；先前 18 次 exact。该臂收敛、不变量通过，未进入固定 Verifier。
程序级网络路径为直连：experiment.json execution.network_mode=direct；variant_execution.py
创建子进程时清除所有 `_PROXY` 变量，worker.py 先安装空 ProxyHandler({}) 后调用原 CLI；
Provider 用 urllib.request.urlopen。此失败是已返回响应里的工具参数 JSON 解析错误，
没有证据支持“又走代理”的判断；未进行包级网络抓取，不能据此讨论外部透明中间设备。

缺陷在实验批次停止时机：外层 run_validation_pairs.py 只在原双臂 CLI 返回后读取整对，
Multi 失败后原 variant_execution.py 已开始 Single。发现时立刻中止，系统 Python 进程归零；
Single 原库留下 8 条 attempt-start、7 条 exact attempt-end，另 1 条 started 未结束。
不能把它记作 Single 失败或配对完成，原目录不覆盖、不重试。后七题没有启动。
该次新增已知费用：Multi 0.395668 元、Single 已知部分 0.085320 元；两条用量未知
各保留 14 元试次占位。累计已知 12.344993 元、未知占位 101 元，合计 113.344993 元；
剩余 6.655007 元不足再为两臂预留 28 元，也不足单臂预留 14 元。
扩大样本计划为 8 对，完整完成 0 对；当前只有早前 astroid 单对模式结果。
要继续这类对照，需先在拥有双臂顺序的原 owner 接入臂间 Provider/未知用量停止检查，
并解决可核账费用问题；本轮不绕开停止条件，不擅自增加预算或改写失败分类。
原证据、状态和费用见 mode-validation-v2-stop.json / expense-status.json。
