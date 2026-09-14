# DA-13：四类完整真实任务观察

原 Product 门禁 3/4，自然委派 0；37 次 qwen-plus 直连、308480 exact tokens，无连接失败。没有 single 对照或独立语义评分，不能解释成多 Agent 收益。

- [冻结合同](contract.json)：四题、调用上限和原源码/脚本/材料/沙箱摘要。
- [当前状态](status.json)：真实结果、用量、重放与边界。
- [原驱动观察](observations.json)：各题任务结果、报告可见状态、用量与清理；原 pending 语义字段保留，不手改正式评分。
- [真实沙箱预检](preflight.json)：四题各缺交付失败、参考结构通过。
- [原日志副本重放](audit.json)：45 份请求、四个原数据库不变、37 份真实主方请求均带委派工具。
- [实现 Agent 语义核读](semantic-notes.json)：不属于独立裁判或用户批准。
- 实际交付：[双主题调查](answers/parallel-investigation/answer.json)、[草稿复核](answers/frozen-review/answer.json)、[版本读取](answers/simple-read/answer.json)。依赖题未交付，不补造答案。
- [最初环境阻塞](environment-block.json)：当时零调用；用户启动后恢复，不能与最终状态混淆。
- [执行记录](../../../deal/039-valuable-delegation.md)与[合同](../../../plan/TRACEHARNESS_DA13_VALUABLE_DELEGATION.md)。

本地完整 Session/Effect、CAS 和隔离 Git 位于 `.traceh/da13-real-1`，离线复核副本位于 `.traceh/da13-audit`。三个分析题的结构参考不是语义 gold。不同阶段的旧成绩没有合入本轮，也没有修改旧实验结果。
