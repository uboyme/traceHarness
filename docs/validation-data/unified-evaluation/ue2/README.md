# UE-2 真实八条证据

Run：`b61ef525-435f-4d12-88b1-9a69b639c4b6`。模型 qwen-plus，独立进程显式禁用 urllib 代理发现。
这是单来源隔离的主线验收，不是新的 72 题成绩或质量提升对照。

- [summary.json](summary.json)：实际运行、调用数、来源和用量。
- [report.json](report.json)：原运行报告，8 条 assessment 全部保持 pending_review。
- [agent-review-advisory.json](agent-review-advisory.json)：Codex 逐条预审观察；没有冒充真人判断或导入正式评分。
- [independent-replay.json](independent-replay.json)：关闭后重新打开 10 个 Session，50 份请求全部重放，不调用 Provider。
- [live-run.zip](live-run.zip)：完整原 run，包含原 SQLite/CAS、冻结源码和材料；可解压到新目录后用 eval --review。
- [review-package.zip](review-package.zip)：离线审阅包、逐条原始目标事件和空评分模板。
- [artifacts.json](artifacts.json)：上述原始工件摘要；本 README 不包含在该冻结清单中。

4 个正例均取得回答前派发的正文证据。预审发现 h-absent、s-absent、m-revoked 的否定结论范围过大；
这些不是重放/越权失败，也不在本阶段修改 Agent 行为。负例是否联合通过需要真实人工按冻结 rubric 判断。
原始报告未导入人工分数；不把机器匹配或本次 Codex 预审统计当最终人工评分。

真实运行之后仅补充了离线 review 的原运行路径和目标事件导出，未改变 Runtime、准备或机器证据评分逻辑。
运行始终以自己的 source.zip 和 frozen.json 为准；不会把后续源码偷偷回填到旧运行。
本目录没有密钥、用户真实会话或真实配置文件。里面的项目、ID、代号和程序均为显式测试材料。
