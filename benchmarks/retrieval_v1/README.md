# Frozen retrieval baseline v1

这是显式评估样本，不是系统默认项目。当前根 manifest protocol 为 **3**；`corpus.json` format 为 **1**，
SHA-256 固定在 task_settings.retrieval；dataset 独立绑定。候选运行前已冻结 11 条查询、相关性、九类阈值、K=3、Context 上限和耗时上限。
全部使用同一 initial tree、同一 Verifier 和 single arm；单样本不作统计显著性结论。

运行仍使用 `traceh eval benchmarks/retrieval_v1 --output <新目录>`，Provider/model 通过该命令的
显式参数选择。当前固定验证协议为 3，运行时还需 `--sandbox-config <宿主策略文件>`；
沙箱使用每个 attempt 原来的账本/CAS，没有配置就拒绝进程执行。每个样本的模型任务由 initial/README.md 说明；query 通过正常 requirement 输入。
本目录 corpus 和 judgments 不复制到模型可写工作区。

Product dataset 当前为 format 3，每题显式绑定 `initial_tree_limits` 的文件数、单文件 bytes 与总 bytes；
旧 format 2 拒绝，既有运行目录不会自动迁移。根协议与 corpus 格式不因这次切换改变。

先在宿主环境显式安装 [typed Skill 示例包](../../examples/plugins/traceh-reference-skills/README.md)。
安装并不自动启用。attempt 按 manifest 先启用两个插件，再退役旧插件，选择当前 Skill；缺失插件、
目录摘要不符或 corpus 字节变化都拒绝。这里不使用第二个 Runner 或评估专属 registry。
当前基线固定 Unicode 版本，跨解释器版本不匹配会拒绝，不能静默改写冻结配置。

报告中的每个 observation 都来自真实 Session/Step 的冻结 Context；Provider 重试不重复计数。
`candidate_ranking` 是候选，`injected` 才是实际输入；相同 kind/id 合并，任一实际披露 tier 可满足
judgment，排名保留首次身份位置。`precision` 是 precision@K，`context_precision` 是全部实际
注入身份的精度；不相关的已注入身份仍进入分母。zero-hit 要求没有注入 Skill 或 Memory。
每个 dispatch（包括没有人工题目的角色）都检查失效/退役/跨项目隔离；`scope_violations` 必须为 0。
没有观察到的题目保留 `unproven`，未 dispatch 的 Step 保留 `unavailable`。Product 成功与检索质量
独立列出，无阈值只测量；每 attempt/role/query 的均值只作描述。

exact、lexical、hard-negative 和隔离类别的阈值按 corpus 中数值执行。semantic 样本使用缺少词面
重叠的表达，词法基线最低分显式为 **0**，用于记录缺口，不等于语义检索已达标。
semantic/reranker 仍为 null，后续启用必须先冻结同条件比较及增益/退化标准。

C3 已用当前 Runtime 重跑原 11 题并独立核对 SQLite，原阈值全部通过；随后四个预注册本地模型候选
在原语料加八条中英文改写上均未达到语言质量/增益门槛，因此继续关闭。原 benchmark JSON 未改。
详见 [C3 验证记录](../../docs/validation-v0.9-stop-c-c3.md)；离线候选试算不冒充正式 Runtime 语义接入。

首次本地网格为 Product 11/11、隔离违规 0，另有 5 条 precision/zero-hit 未达原阈值。
共享检索的精度整改与冻结复验已完成：完整标识符/路径查询不回退到组成词，来源收据记录覆盖，
只有最终预算通过的自动块才排除严格子集候选；相等/互补覆盖和显式披露仍保留。该修订没有改动本目录的
benchmark/corpus JSON、query、judgments 或阈值，也没有按这些样本的名称添加例外。
该精度阶段使用 Session 5、Context 4、f4-context-policy-v2、来源 receipt 2；原配置字段结构不变。协议决定见 [ADR-0047](../../docs/adr/0047-literal-query-coverage-admission.md)，
同冻结输入复验 Product 11/11、原阈值 quality_passed 11/11、隔离 0，原五条失败已达标；四条 precision
及全 Context precision 从 0.5 到 1，Recall/MRR 保持 1，退役目录题 zero-hit 从 0 到 1。
semantic 题的 Recall/MRR/precision 仍为 0，只满足原词法底线，不表示语义能力或泛化精度 100%。
实测结果与 16 个具名文件 303 项定向验证见 [F5 验证记录](../../docs/validation-v0.9-f5.md)。

成本报告区分 Context 准备时长、索引准备/重建时长和总 seeding 时长。索引大小是原 corpus manifest
的逻辑 item_count/item_bytes，不冒充物理 SQLite 大小；纯排序延迟尚未单独计量。
Python、SQLite、Unicode、OS 和机器架构一并记录。History 仍按当前 Session 的 exact provenance
定向测试验收，不计成跨 Session 原文检索。

本轮只通过源码 Entry Point 的定向测试运行该基线，没有执行 Wheel/离线安装、真实 Provider、
全量或 L2–L4。Release Stop C 与发布门禁独立。


后续 [ADR-0048](../../docs/adr/0048-skill-navigation-and-real-provider-disclosure.md) 要求章节显式标题和
说明，当前协议切到 Session 6 / Context 5 / renderer v5 / tokenizer v3，旧 Session 1–5 拒绝。
目录协议改变后，原 catalog digest 会正确拒绝；2026-09-08 只重冻结 `plugins.catalog_digest`
（`1b9d825b…151ef5` → `4a37d125…5b946b`）及 manifest 的 corpus SHA-256
（`a1822e75…3f714e` → `eef440b9…260f3`）。移除新增导航字段可精确重算旧目录摘要。
原 query、判断、阈值、上限、初始工作区和正文不改；这是显式协议重新绑定，不能称本轮 JSON 字节不变。
本基准仍使用确定性 Provider 验证 Product 检索合同，真实模型自主导航由显式集成测试单独验证；
最终回归与真实结果见 [本轮验证记录](../../docs/validation-v0.9-skill-navigation.md)。

UE-1 迁移仅改变评估外壳和 dataset/run-plan 分离，未改题目、judgments 和评分阈值。
本套仍是 ProductTaskEvaluator 的 F5 指标，不是 UE-2 主动检索旅程。
配置示例见 [run-plan](run-plan.example.json)；旧运行分数保留为当时记录。
