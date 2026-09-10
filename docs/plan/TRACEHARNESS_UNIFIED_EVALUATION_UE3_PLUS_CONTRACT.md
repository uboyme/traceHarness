# UE-3+：检索旅程的离线诊断

日期：2026-09-10。已实现并完成定向验证和原真实轨迹离线复算；承接 [UE-3](TRACEHARNESS_UNIFIED_EVALUATION_UE3_CONTRACT.md)，不进入 UE-4/AO。记录见 [020](../deal/020-retrieval-journey-diagnostics.md)。

## 范围与 owner

在原 RetrievalEpisodeEvaluator、报告及离线 review/assess 接入同一组诊断函数。输入只来自原 Session 事件、
原请求、宿主 expectation 和既有派发证据；原评估器、Reader、Projection、Runtime、Sandbox 及评分规则保持原职责。
不新增 CLI 动作、业务事件、检索策略、人工评分字段或第二事实源，不把诊断结果送给被测模型。

## 观测口径

- 相关来源候选：本题已知来源容器是否出现在回答前成功请求中。目录/摘要只证明来源出现，不证明具体正文。
  Memory/Skill 按明确 ID；History 通过原 History Reader 的叶引用定位；Output 按原准备 Effect/digest。
  直接获得足够正文也能证明来源出现，不要求先搜索。没有可靠映射或完整可核对轨迹时保持 unknown。
- 足够证据派发：复用原评估器的派发证据及成功请求绑定，不把 accepted 回执、未派发 Context 或失败 Attempt 当成正文。
  完整有效搜索片段可满足此项，不强制额外 read。进入请求不等于模型已理解或使用。
- 回答判断：展示原 trial assessment 与 provisional 匹配，两者分开；不创建另一评分器，也不自动消除 pending_review。
- 读取请求：列原工具调用、完整参数、结果状态、错误及请求 tier；source_views 保留原版本和章节/页 provenance，输出读取列原字符范围。回执返回不等价于正文送达。
- 负例：正向证据指标为 not_applicable。展示实际查询、搜索域、字段、扫描量、分页游标与原回答，提醒人工核对否定范围。
  查询结束不等于语义全库不存在，不引入强制遍历或消费率门槛，不用中文关键词规则自动判断过强否定。

状态为 observed / not_observed / unknown / not_applicable。not_observed 只表示该条受观测轨迹未出现，不推断库中不存在。
诊断只描述缺口，不单凭缺口断言是模型、索引或预算的根因。不存在完整相关性标注时不报告完整 Recall/MRR。

## 落盘与验证

新执行报告带派生观测及可读表格。已有运行通过原 --review/--assess 在新目录生成 diagnostics.json/md；
不修改原实验、原报告、源码归档或评分绑定；人工评分报告仍由原 judgment 重算。
分析器记录自身版本/源码身份和原 run/evidence 绑定，旧运行和新的诊断产物明确分开。

先离线分析 UE-3 的原 16 条真实旅程，保留全部未知和待审。定向验证正常正文、仅目录就回答、读取失败、
完整搜索片段、失败请求、错误来源、负例范围和取消/未执行；移除关键绑定进行反向验证。
必要时只做少量真实模型补测。不得运行全量、L2–L4 或完整 72；不提交、发布或自动采用。
