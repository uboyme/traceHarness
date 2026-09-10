# UE-3 真实八对 A/A 证据

Run：`366e729d-c6c4-4f08-a50a-8ecb4cc8b211`。现有 qwen-plus 配置直连，两个独立进程使用同一冻结源码，
同样八题、材料、准备文字摘要和运行条件。这是比较框架的验收，不是提示优化收益实验或新的 72 题测分。

| 观测 | aa-reference | aa-repeat |
|---|---:|---:|
| 已执行、关闭的旅程 | 8 | 8 |
| 待人工审阅 | 8 | 8 |
| 正例有实际派发证据 | 4/4 | 4/4 |
| 精确 Token（准备加目标） | 241,784 | 243,031 |
| 工具调用（准备加目标） | 21 | 21 |
| 模型请求 | 51 | 51 |
| 失败的模型请求 | 0 | 0 |

总计 484,815 Token；第二臂多 1,247（约 0.52%），工具总数相同但逐题行为不同。
16 条 assessment 均保持 pending_review；比较 complete=true、status=inconclusive、adoption_authorized=false。
其中 passed_over_planned=0 是“当前已批准通过数为零”，不是 0% 正确率；assessable=0 明确表示还没正式判分。
Skill 负例仍出现从摘要/主题推断不存在的过强结论，原样留存供审阅，没有借这轮 A/A 修改检索策略。

- [summary.json](summary.json)：全部 16 条问题、原回答、证据数、用量和试次观测。
- [comparison.json](comparison.json)：原比较报告，保留全部配对与未知状态。
- [independent-replay.json](independent-replay.json)：关闭后重开 20 个 Session，102 份请求无重放/不变量错误；重比较及 JSON/Markdown 一致，无新 Provider 调用。
- [agent-review-advisory.json](agent-review-advisory.json)：Codex 预审意见，不是人工 judgment，也没有导入正式评分。
- [live-experiment.zip](live-experiment.zip)：原始双臂实验、SQLite/CAS、冻结源码/材料、进程回执和两版原报告。
- [review-baseline.zip](review-baseline.zip)、[review-candidate.zip](review-candidate.zip)：两臂离线审阅包、目标原事件与空评分模板。
- [artifacts.json](artifacts.json)：以上文件字节摘要；README 不在该清单中。

可以把完整实验解压到一个新目录，再用 `traceh eval --compare <解压目录> --output <新的比较目录>` 离线核验。
真实人工评分应分别审阅每臂，并按 [UE-3 合同](../../../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_CONTRACT.md)
提供明确 assessment 文件；不编辑原报告，不根据文件名猜最新评分。
测试中的故事、ID、种子和模型均来自显式实验输入，不是通用代码默认值。
归档前递归检查了实际凭据字节和非空 .env，未含实际密钥；没有复制用户真实聊天记录。
