# DA-11 独立决定输入证据

- [summary.json](summary.json)：原 9 次真实调用摘要，不是完整 Product 评分。
- [audit.json](audit.json)：来源与派生请求摘要、原数据库不变检查、提案原文、预期分类和分工核读限制。
- [合同](../../../plan/TRACEHARNESS_DA11_INDEPENDENT_DECISION.md)与[说明](../../../deal/037-independent-decision-input.md)。

原请求 0/3 有效决定；两个独立输入条件各 3/3 有效决定且分类符合预期。两份 separable 提案尚未证明清楚的独立并行分工，不能当作实际协作通过。共 9 calls、30509 exact tokens、0 Provider failures；没有执行 Tool 或创建助手。

完整派生请求、原响应、冻结计划与诊断代码位于本机忽略目录 `.traceh/da11-independent-1`，这是一次实验位置，不是生产默认路径。原 DA-9 数据库只读且摘要未变，连接秘密不落盘。每格一次、没有留出或正式语义裁判；核读由本轮实现 Agent 完成，不替代人工 Promotion。
