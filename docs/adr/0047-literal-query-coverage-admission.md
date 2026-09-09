# ADR-0047：完整查询字面量与自动参考覆盖选择

- 状态：接受，v0.9-F5 检索精度修订；不表示 Release Stop C 或发布通过。
- 日期：2026-09-08。
- 延续：[ADR-0045](0045-qualified-reference-retrieval-and-history-observations.md)、[ADR-0046](0046-shared-context-governance-and-frozen-retrieval-evaluation.md)。

## 问题与边界

冻结的十一条检索评估中，五条 precision/zero-hit 未达到原阈值。FTS 的 OR 召回将完整标识拆成
普通词，并让所有正分候选进入最终预算；正确的 exact lane 无法阻止另一条 lane 的部分词面误命中。
项目隔离和原权限没有失效，修订属于共享检索和原 Context 自动选择 owner。

冻结 corpus、query、judgment、阈值和 evaluator 不变。不使用样本 ID、领域词典、停用词、隐藏分数
阈值、固定 Top1 或语义模型；不改变人工选择、Memory authority、显式披露、来源和资源生命周期。

## 决定

1. `tokenize()` 在现有 NFKC/casefold、中文单字/双字和普通词索引上保留完整代码字面量。
   `query_terms()` 保持点、下划线、斜杠等连接的完整查询段；有字面量的查询，候选至少命中其中
   一个完整值。多标识查询仍允许各结果分别覆盖一个标识，不要求每条资料包含整个查询。
   连续段的尾连接符保留。只有普通单词后的一个句号视为散文标点；需要该尾点作为标识时使用
   成对引号或反引号。已有内部连接符的段末尾句号也保留，避免猜测缩短标识。
2. 每个来源的 receipt 新增 `coverage`，按来源 fusion 顺序保存 `{identity,terms}`。覆盖来自
   查询词与合法索引元数据的交集，加上实际命中的 exact 字段。Skill resource path 未进入 FTS，
   但合法 exact 匹配可提供覆盖；section/resource 正文仍不参与自动检索。
3. 保留来源内 BM25 与有理数 RRF，统计仍只来自本来源的 eligible corpus。跨来源候选先按覆盖
   词项数降序，再按原 RRF 和稳定内容身份排序。完整 candidate/lane/coverage 证据保留在 Context。
4. 原统一预算 owner 按排序逐块尝试。只有已实际装入的自动块，才能以严格覆盖超集排除后续块，
   原因记 `query-dominated`；相等或互补集合保留。超预算或不可用块不能压制有效小块。
   显式 Skill/Memory 披露仍优先且不参与自动支配；History 的顺序与预算规则保持原状。
5. 原历史 reader 从 frozen Skill catalog/selection、Memory authority 前缀验证覆盖，重放只消费
   当时的 receipt 和正文，不重新查询索引或读取当前资源。没有第二 reader 或可变事实源。

## 唯一协议切换

| 身份 | 当前值 |
|---|---|
| Session marker | 5，旧 1–4 在原入口拒绝 |
| Context outer / source receipt | 4 / 2 |
| Context policy | `f4-context-policy-v2` |
| Index tokenizer / ranker | `traceh-lexical-v2` / `eligible-bm25-v2` |
| Renderer | `context-json-v4`，渲染包装未改变 |
| SQLite / M3 / Benchmark root / corpus | 2 / 2 / 2 / 1，保持原值 |

Context 十二项、来源十五项配置字段不变。旧 corpus key 不能在新分词身份下继续证明来源，因此
明确拒绝旧 Session，不静默解释、迁移、删除或重写旧数据；使用新的数据目录开始新会话。

## 取舍与验证

这是有边界的词法选择：未知礼貌词不会像全查询 AND 一样阻断召回，但更多偶然查询词可能压制
有价值的部分覆盖；相等或互补覆盖的噪声仍可能保留。词法底线为零的语义题继续只记录能力缺口。
对预算取舍、完整路径、退役/未知 ID、中文查询、相等/互补覆盖、显式披露、来源篡改和取消使用
非冻结样本的公开 Runtime/实际 Provider 请求验证。关键保护需反向验证，并在同一冻结评估上复验。
实测数字维护在[验证记录](../validation-v0.9-f5.md)，不在此承诺未运行的发布门禁。
