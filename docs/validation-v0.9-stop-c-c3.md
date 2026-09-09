# v0.9 Stop C / C3 本地混合检索筛查

- 状态：C3 评估与限定确认完成；四个候选均未达到接入门槛，按冻结规则保持关闭。
- 范围：[Stop C 执行计划 §4](plan/TRACEHARNESS_V0.9_RELEASE_STOP_C_EXECUTION.md#4-混合检索的边界)。
- 当前生产仍为 Session 9 / Context 8 / renderer v8 / policy v5；semantic/reranker 未接入。
- 本次不运行全量、L2–L4、安装、Wheel、真实 Provider 或发布操作。

## 1. 评分前冻结

原 [retrieval_v1](../benchmarks/retrieval_v1/README.md) 的 11 条 query、judgments、阈值、K、预算及
获准语料保持不变。新增八条中英文语义改写属于单独的可行性集合，不更改原 benchmark 的成功线。
原 Runtime 使用确定性 Provider，真实模型指本地 embedding/cross-encoder 推理；这与 C2 的真实
聊天模型自主导航验收是不同证据。

显式本地资产为 `all-MiniLM-L6-v2` 快照 `c9745ed1d9f207416be6d2e6f8de32d1f16199bf`，以及
`ms-marco-MiniLM-L-6-v2` 快照 `c5ee24cb16019beea0893ab7796b1df96625c6b8`。
各资产文件 SHA-256 与 402 个源码、测试、语料文件已在 `c3-screen-frozen.json` 中冻结，先于任何候选
评分。两模型纳入冻结的本地资产分别为 91,607,178 和 91,815,758 bytes；只读显式 safetensors，
不自动下载，不将模型名、本机路径或样本值写入产品默认配置。

完整预注册输入见 [cases.json](../tests/local_retrieval_screen/cases.json)：

- 原非语义要求零退化；原语义题 Recall、MRR、precision 均为 1。
- 新增中、英文各四题分别要求平均 Recall、precision 至少 0.75。
- 全部九道语义题的平均 Recall 相对原词法至少提高 0.25；scope 违规必须为 0。
- 暖查询 p95 至多 500 ms；两模型共同冷加载与向量重建各至多 120,000 ms。
- 候选所需模型资产至多 256 MiB；float32 向量每项至多 4096 bytes。

固定比较 cosine 阈值 0.5、0.6、0.7，以及 cosine 0.5 加原始 cross-encoder logit 至少 0。
模型路径、最大 token 长度、池化、归一化、维度、CPU/dtype 和线程数显式固定。

## 2. 单一事实源与筛查边界

[capture.py](../tests/local_retrieval_screen/capture.py) 原样观察原 ProductBenchmarkRunner 与
ContextInputService，使用原 Plugin 生命周期、Memory authority、SQLite 索引、预算和评估报告。
只有已经通过原资格过滤的 Skill 顶层可检索元数据、Memory 生效短事实进入编码器。
不读取隐藏 Skill 正文，也不向模型编码 foreign、revoked、superseded 或 retired 内容。

[screen.py](../tests/local_retrieval_screen/screen.py) 重现原 11 题捕获的实际 Context，再用同一份
获准语料离线评估额外改写。额外题的 FTS 命中来自冻结词频表，不能称为新的真实 Runtime dispatch。
原词法已准入或查询含完整字面锚点时保持原结果；其余空结果用固定模型阈值产生候选，再使用原 K、
Context 准入和字节预算。筛查允许零词面覆盖，但假设结果不写入 Session，不伪造当前协议的合法收据。

当前生产收据要求词面覆盖，融合顺序与预算也使用覆盖关系。筛查通过只表示值得进入原生产主线的
合同与实现改造，仍需来源证明、索引身份、重建、失败/取消、重放及原 Runtime 验收，不能直接启用。
候选失败则按预注册规则保持关闭。空结果补充也无法处理“词法已经误召回”的情况；不掩盖这一限制。

## 3. 验证记录

筛查的三个定向检查已通过（1.33 秒）：原 Runtime 提供实际获准 block，测试确认空词面候选可进入
假设视图，低分、字面锚点和预算不足会排除，已准入词法项保留，重排拒绝生效，Session 不变；
另外两个检查确认源码或资产漂移拒绝。修改范围 Ruff 通过。

原 11 题经原 Runtime 重新采集，Product 与原检索阈值均 11/11 通过，隔离违规为 0。独立核查全部
44 个 Session、44 个 Context 和 44 次 dispatch：捕获与原 SQLite 一致，原来源语料摘要一致，
重放和不变量错误为 0。原语义题仍是 Recall/MRR/precision 为 0 的词法基线。

首次筛查在任何模型评分前因输入查询保留大写、Runtime 已规范化小写而拒绝；修正为复用原 NFKC/
casefold 规则，缺失或歧义仍拒绝。四项定向检查通过（1.32 秒），代码重新冻结为 v2；独立核对确认
题目、资产、阈值均未变化。首次失败日志保留，没有重跑或改写原 Runtime 的正确捕获。

分词器只读诊断发现四条中文改写分别有 10/13、10/12、6/11、7/8 个未知 token；英文四题均为 0。
这是本地资产与输入的实际匹配问题，不据此替换语料或改变候选阈值。质量结论仍须完整评分。
筛查与直接相邻检索/索引/失败 owner 的四个具名测试文件共 43 项通过，90.04 秒；源码编译通过。

## 4. 本地模型实际结果与决定

| 固定候选 | 原语义题 Recall | 新增英文 Recall / precision | 新增中文 Recall / precision | 九题平均 Recall 增益 | 暖查询 p95 |
|---|---:|---:|---:|---:|---:|
| cosine ≥ 0.5 | 1 | 0.25 / 0.25 | 0.25 / 0.125 | 2/9 | 12.56 ms |
| cosine ≥ 0.6 | 0 | 0 / 0 | 0.25 / 0.125 | 0 | 11.40 ms |
| cosine ≥ 0.7 | 0 | 0 / 0 | 0.25 / 0.125 | 0 | 12.10 ms |
| cosine ≥ 0.5，cross logit ≥ 0 | 0 | 0 / 0 | 0.25 / 0.125 | 0 | 25.26 ms |

四个候选均保持原非语义阈值与零隔离违规，但都未达到新增语言质量或总增益门槛。
最佳候选只补回原 `vehicle fuel` 和新增英文汽车燃料题；中文的一条召回本来就来自词法，不能算成
向量新增能力。中文“函数抛异常”的词法误召回也原样保留，证明“只补空结果”不能修正已有错误。
这些字符串均为明确合成评估输入，不是产品默认值或分支条件。

实际两模型共同加载为 747.69 ms（不含 Python 导入），6 条向量重建为 100.73 ms；向量 9216 bytes、
`.npy` 文件 9344 bytes，两次重建在 1e-6 绝对容差内一致，无截断。每个候选保留 19 题 × 5 次
暖查询计时，CPU float32 的成本满足原上限。速度通过不能抵消质量失败。

独立评分核算重新从记录分数排序，重用原预算与评价函数，验证全部 76 条候选观察；最大实际试算
Context 为 2059 bytes。核算器首次误用预算结果字段名，改为原 `rendered_bytes` 后通过；原评分
数据未改。完整机器结果、每题分数、资产身份及原日志摘要见
[C3 数据](validation-data/stop-c-c3-local-retrieval-2026-09-08.json)。

按执行计划，C3 结论是当前本地资产/表示/固定候选不具备接入条件，semantic/reranker 继续关闭。
没有生产代码或协议变化，没有依赖安装、模型下载、第二索引事实源或隐藏模型默认值。
这一结论不等于所有向量模型无效；未来若换用明确的多语言资产并调整候选准入，仍须按原质量要求
重新冻结比较，不能只加一个 RRF 分数就称完成。C2 同一生产源码及其真实聊天模型证据仍适用。

最终核对 244 个生产模块与 C2 接入验证时的字节摘要相同，402 个冻结文件及模型资产无漂移。
compileall、修改范围 Ruff、diff-check、新文件空白/秘密形态/样本硬编码检查通过；3296 项仅收集，
没有执行全量。正式版先更新第 2、7.9、7.10、15 节，再同步通俗版；编号、相对链接与围栏检查通过，
生产流程 Mermaid 继续准确表示唯一词法主线。本阶段未运行 L2–L4、安装、Wheel、真实 Provider 或
发布门禁，也未修改 Git 历史。下一步 C4，再进行 C5 独立审查；Release Stop C 尚未完成。
