# UE-3：冻结文本候选、独立进程与配对比较

日期：2026-09-10。范围：UE-3；实现与定向/真实 A/A 验证完成，结果见 [记录 019](../deal/019-unified-evaluation-ue3.md)。真实语义评分仍待人工审阅。
承接 [统一设计](TRACEHARNESS_UNIFIED_EVALUATION_AND_OPTIMIZATION_DESIGN.md) §8、§10、§11；
不进入 UE-4 全题验收或 AO，不改变 MCP/Workflow 路线，不运行全量或 L2–L4。

## 1. owner 与入口

沿用唯一 `traceh eval` / EvaluationRunner。单变体继续原试次循环；双变体由 variant_execution 持有两个
顺序执行的子进程，每个子进程仍调用同一个 EvaluationRunner。没有新的 AgentLoop、业务事实库或安装路径。
worker 只加载宿主复制并校验的源码；原 evaluator、Runtime、Session、Tool、Sandbox、Product 继续拥有执行与收敛。
源文件和进程隔离针对可信本地文本候选，不是恶意 Python 的安全沙箱。工具执行仍进入既有 Sandbox。

## 2. run plan 与候选

根 benchmark 保持协议 3；run plan format=1 在现行合同内支持两种明确模式：

- current 一臂，comparison=null；
- baseline、candidate 两臂，按此顺序执行，variant_id 不同；必须显式填写 comparison。

每臂仍为 variant_id/role/source。baseline 的 source 为 current；candidate 可为 current（A/A）或
精确 `{file, sha256}` 的 JSON 候选引用，路径按原输入根规则约束。
双臂 execution 为 sandbox_config/max_trials/timeout_seconds/network_mode/shutdown_seconds。
首版 network_mode 仅支持 direct；使用私有子进程环境并显式禁用 urllib 环境与系统代理发现。
max_trials 计算两臂的全部计划槽位；timeout_seconds 是整个双臂执行期限，shutdown_seconds 为协作关闭期限。
没有失败重跑、并行执行、冷恢复或跨协议比较。

候选 format=1，精确字段为 base_source_digest、edits；每项 edit 为 file/selector/old_sha256/new_text。
file 相对 traceh 包目录。允许的节点由宿主在 variants.EDITABLE_TEXT 中固定：

| 文件 | 字符串节点 |
|---|---|
| tools/reference_search.py | HistorySearchTool、SkillSearchTool、MemorySearchTool 的 description |
| tools/output.py | ReadToolOutput、ListToolOutputs、SearchToolOutput 的 description |
| runtime/prompt.py | _REFERENCE_GUIDANCE |

源码摘要绑定当前完整 Python 源码清单，包含未提交字节。old_sha256 绑定 AST 字符串值的 UTF-8 字节。
宿主按 AST 节点的字节范围替换为正确转义的字符串，再验证整棵 AST 除允许值外完全相同。
拒绝错基线、未知/重复节点、整文件替换、grader、dataset、预算、权限、schema 和可执行函数修改。
动态限额拼接函数不在候选范围。A/A 用 current，不伪造空候选或重新命名材料。

## 3. 冻结与生命周期

experiment.json 是双臂实验输入，绑定基线源码、材料归档、原 plan/输入文件、候选、两臂源码和派生计划、
解释器/平台/SQLite/已安装依赖清单、模型与沙箱条件以及所有试次。每臂仍写原 frozen.json、事件、CAS 和报告。
依赖清单是环境身份观测，不是可重建依赖锁；远端模型 revision 未提供时保持未知。
worker 从冻结 plan 通过原 CLI 装配内置 Provider；不传递父进程 Provider 对象的可变状态。
Key 只从原加载器解析并供私有进程继承使用，永不写入冻结物、请求参数文件或日志。子进程 stdout/stderr 不导出。

父进程取消先通过私有 stdin 控制管道取消原运行 Task，等待 Runtime、Store 和 Sandbox 收敛。
重复取消等待同一关闭任务。超出关闭期限才复用 converge_process 终止直接子进程；强制退出不证明业务资源收敛，
保留 incomplete/unproven，停止后续臂。不能把子进程退出码或成功启动等价成测量完成。
每臂保存 process 和 worker-receipt，绑定 PID、确切请求、实际加载源码、环境和收敛结果。
worker-receipt 同时绑定原 report 的摘要，父 execution 再记录回执摘要；离线评分不能直接修改原报告。

## 4. 比较与人工审阅

comparison.py 不运行模型或工具。公共 evidence.py 核对原 frozen、原事件流摘要、源码与材料归档；
review 与 comparison 共用此验证，人工评分仍从不可变 judgment 重算，不接受直接编辑派生报告。
配对按 case/group/material digest/material seed/replicate/requested mode；variant 是对比轴。
不按 resolved mode 或成功样本交集挑选分母；失败、取消、未开始和待审均保留。
检查两臂共同条件相同、源码差异只来自允许候选，不要求实际 prompt 字节相同。
准备阶段生成文字不同单独标识，不能未经等价性证明就归因于目标检索策略。

comparison 精确字段：format=1、min_pass_gain、max_token_ratio、max_tool_call_delta。
后三者可为 null，表示只描述、不设对应门槛。pass gain 和工具增量为非负整数，token ratio 为非负有限数。
记录全部计划分母、可判定数、分组结果、gain/loss/unchanged/unknown、执行失败、原 token/调用/重试与阶段用量，
成本和正确性分开。未知/估计 token 不能冒充精确成本满足门槛；保留已知小计。
Product 的 auto 路由分布、业务统计保留在各臂原 task_report；比较通过绑定的原报告核查，不把 resolved mode 改作配对条件。

状态为 not_comparable / inconclusive / improved / regressed / mixed / no_change。
条件或证据不符为 not_comparable；待审、未证明或有必要成本未知为 inconclusive。
质量提升同时成本上升可为 mixed；报告不自行合成权重，也不授予采用、安装、提交或发布权限。
JSON 和 Markdown 从同一结果对象生成；改评分后的比较写新目录，原实验不变。

以下为示例路径，均由用户显式提供：

```powershell
traceh eval .\benchmarks\retrieval_episodes_v1 --run-plan .\local-eval\paired.json --output .\eval-results\paired-01
traceh eval --review .\eval-results\paired-01\arms\01\run --output .\eval-results\review-baseline
traceh eval --assess .\eval-results\paired-01\arms\01\run --judgment-file .\local-eval\judgment.json --output .\eval-results\assessment-baseline
traceh eval --compare .\eval-results\paired-01 --assessments .\local-eval\assessments.json --output .\eval-results\comparison-reviewed
```

candidate 臂同样单独审阅。assessments.json 为 format=1/experiment_digest/assessments；最后一项把明确的
variant_id 映射到 `{file, sha256}` assessment.json 引用，不搜索“最新评分”。未提供的臂继续使用原待审报告。
离线 --compare 拒绝模型/.env/执行参数；与 --review/--assess 互斥。没有真人判断时不能宣布真实 A/A 语义全通过。

## 5. 验证与停止

定向包含两个实际 worker、候选说明进入真实请求、A/A、故意退步、错基线、越界候选、条件/证据漂移、
未知成本、连接失败、重复取消和报告一致性；反向验证关键保护。真实模型按原设计运行八对 A/A，保留方差和全部失败。
本阶段证明比较与隔离合同，不更新历史 55/72，也不要求真实 A/A 答案完全相同。
