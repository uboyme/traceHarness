# AO-2+ 裁判校准证据

本轮已完成限定实验，**候选未采用，生产裁判已恢复到阶段开始前**。原裁判仍有已知偏差；本轮不宣称校准成功或搜索提升。

12 个冻结开发样本，每条件重复两次：原裁判符合预期 **16/24**，候选 **15/24**。候选误放 6 次、误拒 3 次；开发标签由 Agent 按合同审核，不是独立人工 gold。4 个合成答案和 8 个原答案分别标明，实际 evidence 不更改。

真实直连 qwen-plus **74 次，494,845 tokens**：含首次中止 9 次、最终对照 48 次、原主线复审 17 次。网络失败 0；全部请求可从原账本重放。没有新增检索旅程、拼接旧成绩或授权 Promotion。

| 工件 | 用途 |
|---|---|
| [summary.json](summary.json) | 初次中止、最终对照、原主线复审及含失败成本的完整口径 |
| [cases.md](cases.md) / [cases.json](cases.json) | 12 个样本的原问题、原/合成答案、冻结标签、两次两条件结果与理由 |
| [original-audit.json](original-audit.json) | 原 18 份答案的 Agent 合同审核；不是新评分或人工批准 |
| [native-review-audit.json](native-review-audit.json) | 实验裁判走原主线产生的 18 个判断及审核差异 |
| [original-experiments.zip](original-experiments.zip) | 两次原始实验，含冻结输入/源码、确切执行驱动、74 次调用的 SQLite/请求/响应及复审 |
| [archive-index.json](archive-index.json) | 428 个原文件 SHA、包 SHA、依赖的 AO-2 包及凭据内存扫描结果 |
| [independent-replay.json](independent-replay.json) | 用实验源码重开、逐请求重放、预算与不变量核验，零新增调用 |
| [policy-restoration.json](policy-restoration.json) / [post-restoration-binding.json](post-restoration-binding.json) | 恢复原文件的 SHA，原审阅可读、实验策略审阅被当前策略拒绝 |
| [experimental-policy.py.txt](experimental-policy.py.txt) | 已拒绝的候选源码，供阅读；不被产品导入 |
| [experimental-scope-tests.py.txt](experimental-scope-tests.py.txt) / [experimental-reverse.txt](experimental-reverse.txt) | 候选专项检查及移除准确范围标记后的预期失败 |
| [checks.json](checks.json) | 分阶段定向检查、编译、收集、格式、文档与边界核对 |

复审依赖原 AO-2 运行工件，见 [AO-2 原始包](../ao2/original-experiments.zip)。原 1,523 个文件与其先前归档一致，未替换旧答案/评分。`assessment` 保留绝对运行定位，移动到另一机器后不能改路径和摘要伪装原样通过。

最终实验目录名 `ao2plus-real-20260911-framed`。其 `source.zip` 保存实际实验源码，解包时内容应置于临时 `<snapshot>/traceh/`；以 `<snapshot>` 在 PYTHONPATH 中优先加载，才能重开该策略绑定的 assessment。`baseline-source.zip` 是阶段开始前的原源码。两包是冻结证据，不通过主线增加旧策略兼容分支。

当前研究驱动及使用限制见 [tests/live_optimization/README](../../../../tests/live_optimization/README.md)。离线重放不加载 Key、不调用 Provider；运行真实校准必须显式配置和授权。归档保留原完整输入、原 rubric 与现有 expectation；这些是评审所需的参考数据。额外开发标签与原因只在计划/汇总里，未交给裁判。

实验证据首次完整重放成功，428 个实验文件在检查前后不变；不存在删掉首轮失败才能得到成功的情况。生产恢复后的最终回归独立记录，不声称最终生产重新跑了候选 API 实验。没有运行全量、L2–L4、Wheel、发布或后台优化。

解释与经验见 [记录 025](../../../deal/025-semantic-judge-calibration.md)。
