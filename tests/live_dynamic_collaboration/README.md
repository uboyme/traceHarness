# DA 显式真实实验驱动

`complementary_work.py` 是 DA-12 的六次配对探针：沿用 DA-11 独立输入，只给已有三个字段补 description，回查原来源并验证其他请求内容不变，复用 DA-10 的 Provider/提案检查。失败/取消停止、目录不复用，不执行 Tool 或 Product。两组均 3/3 决定与预期标签，但可拆分工仍未证明，因此没有生产采用。见[记录 038](../../docs/deal/038-complementary-work-contract.md)。

`independent_decision.py` 复用 DA-10 的单步 Provider 探针与提案检查，验证独立目标/证据包。它回查原 Snapshot、调用及成功结果，逐字保留正文和身份，整体替换执行对话，拒绝错配来源；固定三条件各三题、最多九次，失败/取消即停、不可重跑，不执行返回工具或创建 Product。实测原请求 0/3，两个独立条件各 3/3 决定和预期标签；可拆提案分工仍未证明，不采用生产。见[记录 037](../../docs/deal/037-independent-decision-input.md)。

这些模块调用原 EvaluationRunner / ProductTaskEvaluator，不是生产插件或第二套评分框架。
普通 pytest 不联网；真实运行必须显式提供获准的 profile、sandbox、材料和全新输出目录。

`phase_diagnosis.py` 是 DA-10 的受限请求探针例外：它读取已关闭的原日志，在独立目录冻结派生请求，只经原 Provider 观察下一步提案，不执行 Tool，也不创建 Product 或 Workspace，因此不要求 sandbox。四个固定条件共最多 12 次，保留读取与调用配对，失败/取消不重试，原日志摘要变化时拒绝，目录不可重跑。它不是另一个任务评分器。真实结果四条件均 0/3 有效决定，见[记录 036](../../docs/deal/036-phase-transition-diagnosis.md)；没有新增生产能力。

## 自主委派诊断

冻结范围与结果见[诊断合同](../../docs/plan/TRACEHARNESS_DA_AUTONOMOUS_DELEGATION_DIAGNOSIS.md)和[记录 028](../../docs/deal/028-autonomous-delegation-diagnosis.md)。

| 模块 | 职责 |
|---|---|
| diagnosis_materials | 从显式仓库复制实际源码，冻结明确委派/自然复杂/简单任务及结构 Verifier |
| diagnosis | 同一原评估主线执行每条件两次；正向控制必须创建并接受子工作，只有工具尝试不算 |
| diagnosis_audit | 读取已关闭、已 checkpoint 的原 SQLite；核对真正的主 Session 和委派记录 |
| diagnosis_candidate | 经原 AST 白名单生成一次显式文本假设，冻结到独立源码目录，不安装、不采用 |
| diagnosis_evidence | 查看失败助手终止前成功请求实际见过的工具结果；不把它当作完整交接 |

本次 `diagnosis_candidate.TEXTS` 是**未采用的实验材料**，不是推荐配置。它提到的范围阅读超出当时 Product `read_file(path)` 的能力；应保留失败实验的原貌，不用修改文本后重标旧成绩。当前源码与原合同摘要不同必须拒绝复用，历史复核使用同一冻结源码。

运行次序是：离线审计 → 冻结三个条件 → 原版六次 → 有证据时最多一个候选六次 → 关闭全部运行 → 原源码独立重放与语义源代码核读。每条件两次记录都保留，不选择最好的一次。原版与候选任务材料、模型、预算、沙箱和结构评分不变。禁止把缩减题数量、结构通过数或助手数量当协作质量。

`run.py`、`materials.py`、`cases.py`、`optimize.py` 保留原 DA 的 A/A、机制、开发和有限优化驱动；Product 2 的旧记录只能使用对应冻结版本读取。新诊断使用 Product 3，不能混称同一版实验。

## 有界源码读取对照

阅读接口已在独立后续阶段实现，合同见[有界源码读取](../../docs/plan/TRACEHARNESS_BOUNDED_SOURCE_READING_CONTRACT.md)，结果见[记录 029](../../docs/deal/029-bounded-source-reading.md)。本阶段复用同一个 `diagnosis`、原三条件材料和评分器，两臂分别加载冻结的原版与新版阅读源码。没有改动 `diagnosis_candidate.TEXTS` 或任何委派说明；旧候选仍是未采用的历史材料。

十二次真实任务中模型确实使用了行范围读取，但未稳定使用摘要续读，完整报告交接仍为零。结构通过 4/6→5/6，不作为语义正确性或协作收益结论。源码页的确定性检查在 `tests/test_file_reading.py`，真实统计及独立重放见[证据索引](../../docs/validation-data/dynamic-collaboration/bounded-reading/README.md)。

## 额度自主性与真实主子连通性

`budget_autonomy.py` 是明确启动的观察驱动：冻结复杂有空间、简单够用、复杂受限三个单次场景，给每个场景独立真实调用上限，并按主方/助手可见工具集合记录实际调用角色。它不跑基线、语义裁判或自动采用，普通 pytest 不会执行。

2026-09-11 的三次自然观察没有委派助手；后续明确委派检查证明主模型和助手模型均真实运行，`delegate_investigation` 与 `collect_investigation` 成功，但冻结调用上限前没有一条完整 Product task 结束。完整边界见[记录 031](../../docs/deal/031-real-main-child-model-smoke.md)和[精简证据](../../docs/validation-data/dynamic-collaboration/real-main-child/summary.json)。

## DA-6 拆分提示实验

`decomposition.py` 冻结可拆、紧耦合和简单三类单次场景，支持 `--prepare`、`--preflight`、`--run` 三个互斥阶段。预检先在声明的断网 Docker 镜像中证明空产物失败、参考产物通过；真实阶段固定 qwen-plus、每请求一次尝试、每场景独立调用上限，并从关闭的原 SQLite 派生 delegate/collect、主子调用、任务结果、Budget 与 Workspace 收敛。

五轮提示候选共 15 个 trial、121 次真实 Provider 调用，没有一次自然委派；候选均未采用，原生产说明已恢复。普通 pytest 只检查冻结材料和 Verifier，不连接 Provider。结果见[记录 032](../../docs/deal/032-adaptive-decomposition-guidance.md)与[汇总](../../docs/validation-data/dynamic-collaboration/adaptive-decomposition/README.md)。

## DA-7 类型化拆分决策实验

DA-7 曾在显式 adaptive 第一检查点加入 `local`/`separable` typed 决策。确定性候选通过合同验证，但四轮三类真实场景共 68 次 qwen-plus 调用仍没有一次选择 `separable` 或创建助手。按冻结停止规则，生产候选和专用 live driver 已撤回；原始运行证据留在本机忽略目录，精简结果见[记录 033](../../docs/deal/033-typed-adaptive-decomposition.md)与[汇总](../../docs/validation-data/dynamic-collaboration/typed-decomposition/README.md)。普通 pytest 不连接 Provider。

## DA-8 独占拆分界面实验

DA-8 曾让显式 adaptive 首 Step 只公开拆分决定，决定后才展示普通工具。两轮三类真实场景共 54 次 qwen-plus 调用：独占表面按设计生效，但首轮可拆题未收回已创建助手报告，收口修复后的第二轮又把可拆题判为 local。候选按冻结停止规则撤回，专用 live driver 已删除；普通 pytest 不会复跑该实验。精简合同、原摘要和只读复核见[记录 034](../../docs/deal/034-exclusive-adaptive-decomposition.md)与[验证目录](../../docs/validation-data/dynamic-collaboration/exclusive-decomposition/README.md)。

## DA-9 有界侦察后决定实验

DA-9 曾在前两步只显示读取工具，每步最多两个调用，再进入带实际读取回执的独占决定。首组三题共 18 次真实 qwen-plus 调用，只有简单题选择 local 并完成；可拆及紧耦合题切换后仍请求读取，三次未决定后失败。无助手、无网络失败、预算及 Workspace 均收敛。9 个会话的 24 份请求已离线重放；未启动确认组。候选源码、测试和真实驱动已归档并撤回，普通 pytest 不导入或复跑。见[记录 035](../../docs/deal/035-scout-before-decomposition.md)与[验证目录](../../docs/validation-data/dynamic-collaboration/scout-decomposition/README.md)。

## WC-1G 主方收尾观察

`verification_acceptance.py` 复用原 ProductTaskEvaluator，显式执行 prepare / preflight / run；`verification_materials.py` 是新的区间覆盖题和隔离固定检查，不是生产默认。运行前冻结源码、材料与预算，一题一次，最多 16 次真实调用，无重试或基线。检查收尾请求、实际调用与最终声明，并保留原 WC-1F 失败。普通 pytest 不调用真实模型。见 [阶段合同](../../docs/plan/TRACEHARNESS_WC1G_VERIFICATION_REVIEW.md)。

## WC-4 可写助手验收

`writable_acceptance.py` 复用原 EvaluationRunner / ProductTaskEvaluator，按 prepare、preflight、run 顺序显式执行；`writable_materials.py` 是一份两模块遥测题。冻结源码、材料、既有沙箱和上限：主子合计最多 32 次、600 秒、连接 60 秒、零重试、一题一次。失败/取消/封顶后不再调用，已启动目录拒绝重跑；普通 pytest 仅检查离线合同。

本轮真实 18 次调用，助手 Patch 原文读取及整合成功，但完整固定验证失败，未追加真实测试。子实现漏验顶层类型，主方违规新增文件且最终报告不实。机制与功能、语义质量、收益分别判断，见 [记录 054](../../docs/deal/054-writable-real-acceptance.md)。

随后用户另行授权收尾观察修复及一次新验证；复用同一驱动，题目/权限/沙箱/评分字节与首轮一致，仅重新冻结源码，使用独立目录。新轮 19 次调用，新增文件观察进入真实请求、自测使用断言，但模型仍漏验、违规新增文件、报告不实，且没有调用明确整合工具。最终验收仍失败，按约定停止，见 [记录 055](../../docs/deal/055-delivery-review-observation.md)。
