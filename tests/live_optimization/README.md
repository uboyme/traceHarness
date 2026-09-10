# AO-2 显式真实实验

此目录是测试驱动，例子、题目、数值和门槛都不是系统默认。普通 pytest 不调用这里的真实服务。

在仓库 PowerShell 中设置 `PYTHONUTF8=1`、`PYTHONPATH=tests`，运行：

```powershell
python -m live_optimization.run --profile <现有TUI配置文件> --benchmark benchmarks/retrieval_episodes_v1 --output <全新输出目录>
```

profile 经原配置/凭据加载器解析，不打印或保存 Key。为原隔离 worker 只在当前驱动进程的环境中临时传递凭据，退出恢复；父子进程均使用直连 urllib，不走环境代理。

执行前冻结四道开发题（含 UE-4 的目录拒绝后停止、否定范围扩张及正常读取对照）和五个新场景。优化模型只收到开发观察与获准编辑的原文本；新场景不传给它。新场景沿用检索 evaluator 的 `development-regression` 数据协议，独立实例合同声明其为本轮未见验证组，不声称是新的公开统计 benchmark。

一次调用 `run_strategy_optimization` 生成一份候选，复用 AO-1 原双臂运行和原评估审阅。每个裁判调用为独立 Session，origin=model，使用同一冻结 rubric；最终采用仍由人决定。若提案/执行失败或没有候选，则记录并停止；没有隐含重试或手工替换输出。合法候选冻结后，在五个新场景上运行同一 patch，哪怕开发成绩没有上涨，也保留这次诊断验证的完整结果。

控制模型使用显式 token/输出/墙钟限额；本驱动声明最多一份提案、18 次完整 trial（8 开发、10 新场景）、18 次裁判，试验本身另遵守原每题 Step/token 预算。分析、裁判及试验的真实 usage 分开统计；没有 usage 的调用记 unknown。阈值是这次实验的 1 题净增、Token 比例不超过 1.15、工具调用增量不超过 2，禁止运行后改门槛。

输出保存 frozen-smoke.json、原源码/材料、提案调用的 SQLite/请求/响应、两臂原证据与模型审阅。`report.json` 是派生显示；原 `inspect_strategy_optimization`、`load_model_call`、`load_assessment`、`inspect_experiment` 可重开检查。原请求还应通过 `verify_request_snapshots` 单独重放。

不运行全量、L2–L4、安装、Wheel、提交或发布。不把这几题拼进旧 72 题成绩。裁判也是模型意见，最终报告应提供逐题原答案和判断供用户核对。


若驱动在验证批次开始前因计划引用错误退出，可在明确检查后使用 `python -m live_optimization.verify --profile <配置> --experiment <已冻结实验目录>`。它只允许尚未启动的验证目录，核对原策略提案、完整开发审阅、源代码与冻结数据；不重新提案、不重跑开发题，不是生产优化器的自动恢复能力。源代码已改变时明确拒绝，应检查旧工件，不能强行继续。

离线重放不加载 Key、不调用 Provider：

```powershell
python -m live_optimization.reopen --accepted <已接受提案实验> --rejected <已拒提案实验> --output <新的检查目录>
```

它逐个打开原 Session/Effect、核验请求与不变量，核对模型调用和原 assessment，再检查原文件是否变化。现有 assessment 引用绝对运行路径，整体移到另一机器后不能冒用原路径通过审阅重开；SQLite/冻结源码/材料仍可独立重放。不要修改旧原件中的路径或摘要以假装原始核验通过。

实际结果和含失败成本的归档见 [AO-2 数据索引](../../docs/validation-data/unified-evaluation/ao2/README.md)。

## AO-2+ 语义裁判校准（已结束，候选未采用）

`calibrate.py` 与 `calibration_inputs.py` 是对原 AO-2 证据的固定开发实验，不是另一套 evaluator。12 个输入、每条件重复两次，调用原 run_model_call；再走原 review/assess/comparison 补审原答案。不重跑检索。校准时已完成且精确计费的 pending 是一次失败观测，不重试；生产审阅的 pending 即停止规则不变。

候选没有达到冻结门槛，已恢复原 production policy。当前源码执行该历史候选驱动会在加载凭据前明确拒绝 `calibration-requires-frozen-candidate-source`。历史复现应使用归档对应 `source.zip`，在单独目录解包为 `<snapshot>/traceh/`，以 `<snapshot>` 优先于仓库 src 加入 PYTHONPATH；不覆盖当前仓库生产代码。`baseline-source.zip` 与 `baseline-inputs.json` 保存原条件，执行时需作为明确的 before 输入目录中的 `source.zip`、`inputs.json` 提供。真实调用还需已有的原 AO-2 运行目录；运行时须遵循已明确的授权和预算，不把以下语法当默认配置：

```text
python -m live_optimization.calibrate --profile <明确配置> --before <原条件目录> --original <原AO2证据目录> --output <全新目录>
```

仅离线核验本轮记录使用：

```text
python -m live_optimization.reopen_calibration --run <首轮停止目录> --run <最终实验目录> --output <新的检查目录>
```

`reopen_calibration.py` 从原模型调用还原观察，检查输入/预算/绑定/重复身份，验证两条件证据相同，再通过原请求重放与审阅器核验。包含实验 assessment 时要加载相应冻结源码；当前恢复后的生产策略会明确拒绝不同策略绑定。原 assessment 的绝对定位不能伪造迁移。

测试驱动中的样本名、数值、标签和额度都是本次显式合同输入，不是通用默认；标签为 Agent 合同审核，不是独立人工 gold。参见 [AO-2+ 合同](../../docs/plan/TRACEHARNESS_OPTIMIZATION_AO2_PLUS_CONTRACT.md)、[结果与原工件](../../docs/validation-data/unified-evaluation/ao2plus/README.md)及[通俗记录](../../docs/deal/025-semantic-judge-calibration.md)。

## AO-3 真实后台主线

显式授权后，以 `tests` 为 PYTHONPATH：

```text
python -m live_optimization.background --profile <现有连接配置> --benchmark <检索旅程题库> --output <全新隔离目录>
```

驱动通过原配置加载器借用连接，进行隔离 Chat、反馈、原 AO 提案、独立双臂和四次裁判，再独立重放原证据。密钥不写入实验文件；不修改原用户 Session。材料、两题、seed、额度和比较门槛是显式测试输入，不是产品默认。该测试只证明小样闭环，不冒充 72 题统计。见 [AO-3 原数据](../../docs/validation-data/unified-evaluation/ao3/README.md)。
