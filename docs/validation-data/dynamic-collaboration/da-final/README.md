# DA 有限真实实验与工程验证摘要

`summary.json` 是原 Evaluation/AO 及独立重放产物的派生汇总，不是新的事实源。完整解释见 [验证记录](../../../deal/027-dynamic-collaboration.md)，预设额度和停止条件见 [真实合同](../../../plan/TRACEHARNESS_DYNAMIC_COLLABORATION_DA_REAL_CONTRACT.md)。

68 个真实任务 trial；共 777 份模型请求，包含任务、语义裁判和一次优化分析。真实模型未调用委派。基础策略未达到开发资格；一次分析返回 no-candidate，因此未执行候选比较或条件性留出。默认保留 single，没有采用或发布 DA。

模型记录来自原冻结 Product 2 开发 Wheel；当前 Product 3 通过单独的定向、实际 Docker 和干净安装检查，两者不拼成同版本收益结论。没有全量 pytest 或 L2–L4。各测试集合有重叠，不能把 JUnit 数量相加。

完整原始目录留在本地 `.traceh/da-live` 与 `.traceh/d4*`，独立重放在 `.traceh/da-independent`，JUnit 在 `.traceh/da-gates`。本目录仅保存不含凭据的可分享摘要和对应摘要校验值。
