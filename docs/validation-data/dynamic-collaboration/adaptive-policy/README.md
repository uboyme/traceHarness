# DA-14：主方协作说明与真实观察

本轮补主方专属提示，原 Product 门禁 4/4、自然委派 0。42 次 qwen-plus 直连、390648 exact tokens，无 Provider 失败。不能认定协作能力或收益提升。

- [冻结合同](contract.json)：复用原 DA-13 驱动，所以 kind/benchmark ID 保留原材料名称；本目录代表新源码的一次独立观察，不覆盖旧结果。
- [最终状态](status.json)与[原驱动结果](observations.json)：原 semantic_review=pending 保留。
- [真实容器预检](preflight.json)：四题各缺交付失败、参考结构通过，共八次。
- [副本核验](audit.json)：50 份请求重放通过，其中 42 份真实主方请求均含新提示全文，8 份为原产品流程本地控制请求；四份原数据库摘要不变。
- [实现者语义核读](semantic-notes.json)：不是独立裁判或用户批准。
- 实际交付：[双主题](answers/parallel-investigation/answer.json)、[依赖调查](answers/dependent-investigation/answer.json)、[冻结草稿](answers/frozen-review/answer.json)、[版本](answers/simple-read/answer.json)。
- [计划与提示全文](../../../plan/TRACEHARNESS_DA14_ADAPTIVE_POLICY.md)、[执行记录](../../../deal/040-adaptive-policy.md)。

完整 Session/Effect、CAS、隔离 Git 及输入位于 `.traceh/da14-real-1`；重放副本位于 `.traceh/da14-audit`。默认仍 single，不自动采用候选。没有基线重跑、独立语义裁判、全量/L2–L4/Wheel、提交或发行；用户启动的 Docker 保持运行。
