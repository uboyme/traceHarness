# C3 本地检索可行性筛查

这是显式开发诊断，不是产品 Runner、新检索事实源或默认模型配置；不会被 pytest 自动调用，也不访问
真实 Provider。`cases.json` 是候选评分前冻结的试验输入，不进入模型可写的工作区。

`capture.py` 调用唯一 EvaluationRunner / ProductTaskEvaluator，沿用原 11 题、Plugin 生命周期、Memory authority、
SQLite 索引、Context、预算与确定性 Provider。与原具名测试相同，只用源码 Entry Point 替代已安装包
的发现元数据；不安装包。观察包装原样返回原结果，以 ContextVar 隔离每次原 freeze 的记录。
原评估报告、SQLite 和捕获的获准内容保留在显式输出目录。

`screen.py freeze` 在模型评分前冻结源码、测试、原基准、候选标准和本地资产摘要。
`screen.py run` 首先核对摘要，然后只加载显式本地 safetensors，禁止远程代码与下载；本诊断使用
CPU float32、mask mean pooling、L2 归一化、384 维向量。重排器使用原始单一 logit。
本地 Python 环境必须已具备依赖；脚本不会自动安装或补模型。

原 11 题的词法结果必须逐项重现捕获的实际 Context；额外八条中英文改写在同一获准语料上调用原
纯排序与准入函数。额外题的 FTS 命中由冻结的词频表计算，明确属于离线筛查，不冒充新 Runtime
dispatch。Skill 只编码原顶层可检索元数据；Memory 编码原批准短事实，资格过滤先于任何模型评分。

候选是保守的空结果补充：已有词法准入或完整字面锚点时保持原结果；否则用固定 cosine 阈值筛选全部
获准内容，再按固定规则排序、截取原 K 并交给原 Context 准入和字节预算。零词面覆盖允许在此筛查中
参与，但不构造合法生产收据、不持久化假设结果。该候选不能解决已有误召回阻塞语义补充的问题。
通过筛查之后仍必须改造唯一生产主线的来源收据、重放和准入合同，再做原 Runtime 实测，才能启用。
不通过则保留关闭状态；不把“存在 embedding 分数”称为实现了语义检索。

门槛见 `cases.json`：原非语义要求无退化，原语义题 Recall/MRR/precision 各为 1；新增每种语言的
平均 Recall、precision 均至少 0.75，全部语义题平均 Recall 比词法至少提高 0.25；隔离违规为 0。
另冻结 CPU 延迟、加载、重建和资产/向量规模上限。四个固定候选全部保留，不按题名调参。
样本很小，结论只适用于是否进入本次生产接入，不能推出普遍多语言能力。

以下命令是显式开发示例，路径由执行者提供。先设置 `PYTHONUTF8=1`，并使仓库 `src`、`tests`
进入 `PYTHONPATH`。输出路径必须是新的，已有结果不会覆盖。

```text
python tests/local_retrieval_screen/screen.py freeze --embedding <完整本地模型路径> --reranker <完整本地重排器路径> --output <新冻结文件>
python tests/local_retrieval_screen/capture.py --output <新捕获目录>
python tests/local_retrieval_screen/screen.py run --freeze <冻结文件> --capture <捕获目录>/capture.json --output <新结果目录>
```

结果解释必须区分：原 Runtime 已通过的词法基线、离线假设准入的模型筛查，以及尚未接入的生产能力。
五次单查询计时保留原样，包含编码、向量匹配，以及配置启用的候选重排；没有用查询缓存降低耗时。
两模型一起加载的冷耗时是本筛查的保守上界，资产大小则按每个候选实际所需模型计算。

本次四个候选均未达到预注册的语言质量和增益门槛，保持生产语义/重排关闭。原 Runtime 基线与独立
核算通过。详细结果与首次查询规范化对齐失败保留在 [C3 记录](../../docs/validation-v0.9-stop-c-c3.md)。

UE-1 公共报告将原 Product 结果放在 task_report；采集入口已同步，历史离线分析工件保留原格式。
