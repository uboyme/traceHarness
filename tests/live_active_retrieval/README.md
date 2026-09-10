# 主动检索真实验收（AR）

UE-2 起，新协议检索运行使用 `traceh eval benchmarks/retrieval_episodes_v1`。本目录保留旧版本实验和审计脚本，不作为当前运行入口。当前合同见 [UE-2](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE2_CONTRACT.md)。

本目录是显式开发验收，不是产品默认配置，也不会由普通 pytest 自动调用真实 Provider。

`manifest.json` 冻结 24 道自然问题、来源模板、两套领域材料、三组种子和资源/质量门槛。
`contract.py` 只负责清单读取及冻结校验；它不生成模型回答或搜索分数。
冻结证据在 [manifest-freeze.json](../../docs/validation-data/active-retrieval/manifest-freeze.json)，
既有配置解析后的模型身份在 [provider-freeze.json](../../docs/validation-data/active-retrieval/provider-freeze.json)。
测试字段、代号、人数和路径都只属于隔离夹具；生产代码不能按这些内容匹配或选择默认。

基线为提交 `a54d431` 的源码快照；候选为后续 AR 实现。两者在隔离数据目录执行同一清单，
使用原 Runtime/Tool/SQLite/Plugin/Memory authority；History 准备和目标回合使用真实 Provider，
Memory/Skill 准备可通过公开宿主入口完成，工具输出必须来自真实执行，不能伪造返回文本。
所有准备、摘要和目标调用的成本分别记录，输出命令只执行一次；失败运行不删。

目标问题不注入工具名、ID、预期答案或具体搜索步骤。判定读取实际请求、来源和最终答案；
负例还需检查自然语言结论是否明确缺乏证据。不能只检查工具是否被调用，也不能把回答中碰巧出现的
数字当作成功；需要核对字段归属、当前批准状态和答案所依据的确切来源。

实际 runner、source fixture 和逐请求 audit 已接入，第一次评分前额外冻结它们的
代码摘要、具体生成语料与最终装配策略。AR-B/AR-C 已有真实诊断；AR-D 完整网格已收齐并审阅，结论 NO-GO，
详见[最终报告](../../docs/validation-active-retrieval.md)。
不得把参考合同测试的通过称为主动搜索已经实现，或把之前 E/语义复测用量计入 AR。

本计划不跑全量 pytest、L2–L4、安装/Wheel、发布门禁；真实连接仅走已授权的现有配置加载器，
不输出密钥，不改用户 Profile/Session。详见 [执行计划](../../docs/plan/TRACEHARNESS_ACTIVE_RETRIEVAL_EXECUTION_PLAN.md)
和 [ADR-0061](../../docs/adr/0061-active-reference-search.md)。


`grid.py --freeze --baseline-source <基线 src> --profile <已授权 profile> --output <新目录>`
冻结全部实际材料、运行器/支持文件、两臂源码、四类 Context 配置和模型身份。冻结后使用同一目录，分别执行
`--arm baseline|candidate --worker 0|1`；每个 worker 固定处理对应互斥分区的 36 条任务，数据目录互不复用。
基线进程的 PYTHONPATH 指向原提交源码与当前 tests；候选指向当前 src 与 tests，程序会核对实际导入的源码摘要。
已关闭的失败任务不会删除或单独重跑；中断后未关闭的旅程需明确审计。`*-progress.json` 仅提供暂定检查，
不是最终得分，负例仍需要按冻结规则逐条审阅。所有用量同时报告未知或估计计数，不能当作实际零。
最新完整运行记录在 [ar-d-grid-05](../../docs/validation-data/active-retrieval/ar-d-grid-05/frozen.json)；
grid-02 因工具参数上限未说明的真实问题中止，保留失败及中断账本，不能当成完整评分。
修复后按配置生成参数上限，经过正向和反向检查；grid-03 保持原材料、题目、预算与门槛。
grid-03 又暴露大结果收存清空 Skill 控制回执的问题；中止并保留证据后，修复同一 Effect 到 Session 的控制回执投影。
grid-04 继续同一冻结清单，不放宽任何验收门槛；两臂 117/144 条 TLS EOF 失败，候选联合通过 11/72，未达标。
grid-05 在无评分连接探针成功后，按预先冻结的相同两批安排完整复测；无连接错误，基线 21/72、候选 45/72，仍为 NO-GO。
候选源码及支持文件保存在该目录的 `candidate-source.zip`，与冻结原始字节逐项匹配。
`audit.py` 只读原账本、生成审阅包，要求证据实际进入成功模型请求，
不会调用模型，也不会自动判定负例和额外事实。
grid-01 在首次评分前因用量预检修订而废止，真实调用为零。

`reopen.py <网格目录> baseline|candidate <新的备份目录>` 通过只读 SQLite 连接创建独立备份，
再用该臂冻结源码的 SessionService/SurfaceProjector 重放；不启动模型，不修改原记录。
源码归档解压后使用其 `src` 和 `tests` 设置 PYTHONPATH，保留冻结文件的原始字节。

职责诊断阶段的 `output_diagnosis.py` 复用同一 `run_case`，执行显式 `experiment.json` 中的工具输出子集。
运行命令为 `python -m live_active_retrieval.output_diagnosis --arm baseline|candidate --profile <已授权配置> --output <实验目录>`；
PYTHONPATH 必须指向该臂冻结源码及 tests，实际导入文件先通过摘要核对。仅显式调用时联网，不由 pytest 自动运行。
其 `--freeze --reference <grid-05 frozen.json> --baseline-source <原版 src> --output <新目录>`
专用于第一个呈现实验（六题、两个显式测试种子）；后续假设需先单独冻结说明和源码，不能借用旧假设或覆盖旧目录。
两轮记录在 [职责诊断](../../docs/deal/002-active-retrieval-responsibility-diagnosis.md)，结果分别为 4/12 → 3/12、3/12 → 5/12，
均未合入生产。它们不替代 AR-D 的 72 题验收；新种子不等于未见题目，负例原文也未改变。
每个实验保留与 `experiment.json` 内容一致的 `frozen.json` 供原 `reopen.py` 只读复核。
实验候选的改动文件与 grid-05 源码归档组合后须通过逐文件冻结校验；不要用生产 reader 读取不同呈现规则的实验请求。

诊断运行入口和原网格入口一样先将输出目录解析为绝对路径，避免 Skill 资源根在准备阶段因相对路径被拒绝。
拒绝反馈实验见[第三份诊断记录](../../docs/deal/003-tool-denial-feedback-diagnosis.md)：03 轮准备失败保留，
04 轮完整八对任务复测。该轮还使用英文 Skill 与两条正常读取对照；实际夹具以 experiment.json 的 fixtures 为准，
不使用首轮专用的 `--freeze` 选题。候选仅改变 denied 消息的字段呈现，未合入生产，不能把结果加入 AR-D 得分。

## 连续相同拒绝诊断

`denial_loop.py` 复用原真实运行主线，固定四个自然问题和一个显式重试压力例，对照关闭保护与默认保护。压力例不计入检索得分；真实调用需要显式授权与配置加载器。冻结配置、源码、逐例账本及重放结果保存在 [本轮记录](../../docs/deal/004-repeated-denial-continuation.md)。不能把单次小样本当作 AR 发布验收。

## 来源导航修复对照

`output_diagnosis.py` 也用于 [来源导航修复](../../docs/deal/005-source-navigation-and-delivery-semantics.md)：`source-guidance-01` 保留六对正例，`source-guidance-negative-01` 保留随后冻结的两对负例；分别汇报，不把失败负例排除后宣称整体通过。两组生产源码冻结在正例目录的 baseline/candidate-source.zip；旧组用旧协议源码重放，新组用新协议源码重放。

## 证据范围与正反事实诊断

[记录 006](../../docs/deal/006-evidence-bounded-retrieval-policy.md) 保留七组实验：提示重写、后置提醒、一次草稿复核、大结果定位入口、最终导航与读取范围对照。复用原运行器的实际支持范围包含四类来源，不限于文件名所称的 output。每组单独冻结；后续重复的诊断题不能冒充新留出题，正例、负例和传输错误分开报告。

草稿复核只在该组 `runner-as-executed.py` 注入既有 Continuation，临时标记仅属实验，不进入生产。其结果未稳定改善，没有留下默认额外模型调用。其余组通过原 `output_diagnosis.py` 与各臂源码校验运行；使用各自归档源码离线重放，包括未采用的呈现版本。仅 `summary.json` 的正例联合通过可以自动汇总，否定答案必须查看来源范围和原文；TLS EOF 不算语义错误或修复增益。
