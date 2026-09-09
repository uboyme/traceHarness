# v0.9-F5 开发验证与冻结质量基线

> 本记录保留精度阶段原始结果。后续 Skill 导航修订及真实 Provider 调用见
> [独立验证记录](validation-v0.9-skill-navigation.md)：当前协议已切到 Session 6 / Context 5 / renderer v5 /
> tokenizer v3；原冻结检索仅更新新描述符的 catalog 绑定及依赖文件摘要，原题目、判定和阈值不改。
> 本文“输入 JSON 未改”和“未运行真实 Provider”只描述当时的精度阶段。


记录收口日期：2026-09-08。

本记录覆盖 F5 治理/评估开发及检索精度修订，版本仍为 0.8.0；不是 Release Stop C 或发布通过证明。
仅运行具名定向测试，没有全量、L2–L4、Wheel、安装、联网包索引、真实 Provider/API、commit/push/tag/release。
保留既有 F4 工作区改动及用户无关文档。

## 1. 当前实现

Line/Textual 共用治理 service 与原事实 owner；显式 context-config、插件 setup 前 Manifest 审阅、
Memory/Skill/Project 原 CAS 确认、History 原有界展开、实际 Context 读取均接入。
根 benchmark protocol 2 拒绝旧 1；原 attempt 拥有真实 requester/scope/seed-before-host，
Product 原绑定准备角色 Memory index。精度修订后的 SQLite/Session/Context 为 2/5/4，
来源 receipt 2、policy v2，renderer context-json-v4 保持不变；旧 Session 1–4 明确拒绝。
细节见 [ADR-0046](adr/0046-shared-context-governance-and-frozen-retrieval-evaluation.md)。

## 2. 治理／评估实现阶段验证（精度修订前）

| 定向范围 | 已验证结果 |
|---|---|
| Line/TUI、CLI/恢复/环境、Product 配置、旧 Product benchmark、Memory Product 相邻回归，10 文件 | 274 passed，102.86s |
| 共享治理、插件组合迁移、Project scope、两份 Product 架构/合同保护，5 文件最终确认 | 145 passed，56.97s |
| 新 CLI context-config 正向/输入拒绝/恢复路径 | 2 passed |
| 25 文件 Skill/Memory/History/索引/Runtime/Plugin/架构相邻清单 | 首轮 464 passed、1 failed；失败是新 Project 查看遗漏目录，已修复并包含于上方 145 项最终确认 |
| 冻结评估/真实 seed 失败及取消/Product 挂接后索引失败及取消，2 文件最终确认 | 18 passed，864.69s；包含真实 11-attempt 基线复验 |
| 反向保护恢复后，治理/配置/冻结输入与指标最终确认 | 16 passed，3.39s |
| 修改 Python 范围 | 62 文件（含已有 F4 改动）Ruff 通过；compileall src/tests/新示例通过；git diff --check 通过；core 环境 collect-only 3173 项，未执行全量 |

这些清单有重叠，不能相加为独立测试总数。TUI 使用现成的本地 Textual 8.2.8 缓存路径，未安装或联网。
core-only 分支仍经 optional TUI 相邻测试校验，不能用此冒充 clean-input Wheel/离线安装。
最终 18 项证明 seeding 已真实 approve 后失败/取消，调用返回前 Runtime.dispose 已完成、Store 已关闭，
持久审批证据仍可重读；Product 真实 attach/index 已发生后失败/取消会 release 工作区。
无人工评分项的真实 coder Step 仍执行隔离检查，缺题保留分母。11-attempt 复验保持 Product 11/11、
44 个 Step、隔离 0、相同 5 条质量失败，未调整 frozen input/阈值。

两项关键反向验证均按预期失败并逐字节恢复：移除 catalog 审阅保护后，同 Skill ID/version、
不同内容的真实 Generation 替换被旧确认接受；移除 frozen file digest 检查后，文件已漂移的真实
attempt 仍 seed/完成 Product 并产出 evidence。失败不是导入错误或未发生操作；恢复后包含这两项
的 16 项确认全绿。生产示例硬编码/真实凭据形态扫描未命中。

测试开发中两个 seeding cleanup 断言曾错误预期 disposed，实际按公开路径得到 event-store-closed；
测试已改为同时观察原 Runtime.dispose 完成及 Store 拒绝操作。Product 取消夹具曾未放开被收敛任务
等待的 Gate，导致测试挂起；已停止该单一测试进程，改用 cancel 后显式释放 Gate，未改生产 cleanup。
这些不是生产 Finding，也未当作通过证据。

## 3. 冻结基线：首次测量

[基线文件](../benchmarks/retrieval_v1/README.md) 在候选结果前固定。corpus SHA-256：
`a1822e75c1a3caa1d8b52f7799e1f41218c7a50d17fb29ce8ae386da4c3f714e`。
evaluator digest：`ca8345b7881ded9c3dd1364a0bfc817cb16c518ff35f45f8dd55052f6b6b0c89`。
K=3，单次观察；无统计显著性主张。首轮环境为 Windows AMD64、Python 3.12.7、SQLite 3.45.3、
Unicode 15.0.0。11 次 Product attempt 全部完成，共 44 个实际 Step；所有 dispatch 隔离违规为 0。

| 查询/类别 | 首轮指标 | 冻结质量阈值结果 |
|---|---|---|
| goals.code / exact | Recall=1，MRR=1，precision@K=1 | 通过 |
| 中文项目目标 / lexical | Recall=1，MRR=1，precision@K=0.5 | 未通过，多带入一项 Skill |
| src/route.py RouteError / lexical | Recall=1，MRR=1，precision@K=0.5 | 未通过，多带入一项 Memory |
| vehicle fuel / semantic | Recall/MRR/precision=0 | 达到事前词法底线 0；不代表语义能力达标 |
| quasarnevermentioned / zero-hit | zero-hit=1 | 通过 |
| secretneedle / cross-workspace | zero-hit=1，隔离违规 0 | 通过 |
| revokedneedle / revoked | zero-hit=1，隔离违规 0 | 通过 |
| priorneedle / superseded | zero-hit=1，隔离违规 0 | 通过 |
| reference.retired / retired-generation | 旧 Skill 未泄漏，但注入另一项现役 Skill；zero-hit=0 | 未通过 |
| route.error / hard-negative | Recall=1，MRR=1，precision@K=0.5 | 未通过 |
| reference.current / exact | Recall=1，MRR=1，precision@K=0.5 | 未通过，多带入一项 Memory |

首次 5 条未达预设质量线，原因是词面部分重叠候选进入自动注入。第一次评估如实给出 false；
随后的共享规则修订及同冻结复验见第 4 节，没有降低阈值、删除样本或按 ID 写例外。
Product 成功与质量达标分别检查，仍不能代替 Stop C 和发布门禁。

首轮 Context canonical 字节范围 423–2029，最长 Context 准备 3406ms。第一 attempt 的 Skill
索引逻辑 1 条/997 bytes，Memory 索引 5 条/8049 bytes；对应准备及重建为 16ms/875ms。
这些是描述性单次观测，完整报告对每 attempt 单独保留计量。逻辑条数字节不是物理 SQLite 大小，
Context 准备也包含来源校验，不是纯排序延迟。

## 4. 检索精度修订

[ADR-0047](adr/0047-literal-query-coverage-admission.md) 在原共享检索和预算 owner 保留完整查询
字面量、冻结覆盖证据，并让已实际装入的自动块排除严格覆盖子集。没有修改冻结 JSON、阈值、
judgment 或 evaluator。相等和互补覆盖保留；预算失败不压制小块；显式披露不参与自动支配。
它仍是词法启发式，不能证明语义相关性。

非样本公开 Runtime 测试 19 项通过（15.95s），覆盖中文礼貌查询、完整和未知标识、尾连接符、
普通句号与显式引用、跨来源相等／互补覆盖、预算回退、未索引资源 exact、历史覆盖篡改、取消
和显式披露。尾连接符问题由独立审查真实复现为 P2，修复前的三项反例均误注入较短标识，修复后
通过；复核已关闭该项，限定范围没有 P0/P1。这不代替 Release Stop C。

三组关键反向验证均确因目标行为失败，并逐字节恢复源码：

- 恢复碎词回退：已成功检索正常标识后，查询另一完整标识误注入已存在的相似项。
- 移除覆盖排除：真实 Provider 请求多了一项中文弱匹配 Skill。
- 移除覆盖来源证明：重签 digest 的伪造覆盖通过了公开 Session reader。

既有 supersede 回归中的正文改为用反引号界定完整标识，明确正文冒号是分隔符；不再依赖旧 FTS
拆词吞掉尾连接符。Skill 来源覆盖在完整 Session 的历史读取边界验证：Composition 写在 Context
之后，没有为测试新增提前读取 seam；Memory 仍沿原 frozen-source 验证。

最终 16 个具名测试文件 **303 passed in 973.99s**，包含上述 19 项非样本用例、冻结评估与真实
seed/Product index 失败及取消；其中只运行一次完整的 11-attempt 冻结网格。compileall src/tests、11 个本次修改 Python 文件 Ruff、
git diff --check 已通过；core 环境 collect-only 为 3193 项，未执行全量。六个生产修改文件的冻结
样本名称扫描无命中；测试／评估中的具体名称均为显式夹具。

最终冻结结果：Product 11/11、原阈值 quality_passed 11/11，44 个唯一 Step，隔离违规 0、
unproven 与 unavailable_steps 均为 0。原来五条失败全部达标：

| 原失败类别 | 修订前 | 修订后 |
|---|---|---|
| 中文目标 lexical | precision@K / Context precision 0.5 | 两项均 1，Recall/MRR 保持 1 |
| 路径＋错误 lexical | precision@K / Context precision 0.5 | 两项均 1，Recall/MRR 保持 1 |
| retired-generation | zero-hit 0，额外注入现役 Skill | zero-hit 1，零注入 |
| hard-negative | precision@K / Context precision 0.5 | 两项均 1，Recall/MRR 保持 1 |
| Skill exact | precision@K / Context precision 0.5 | 两项均 1，Recall/MRR 保持 1 |

其余词法正例与零命中反例保持达标。semantic 题 Recall/MRR/precision 仍为 0，只达到事前的词法
底线 0，不能称为语义检索通过或总体语义精度 100%。环境仍为 Windows AMD64 / Python 3.12.7 /
SQLite 3.45.3 / Unicode 15.0.0。corpus 和 evaluator 摘要保持第 3 节原值；benchmark.json 摘要为
`247b64050de45c0d8d81800154cbf27d42754f8f66093020cb4612e9efeb5d1f`，与修订前相同。
最终 report.json SHA-256：`5fd9b37491fe486d2dfda2aa66e880a9abacc89b9de20c4b0bd59dc4f8dbf7ea`。

最终 Context canonical bytes 范围 423–1227，最长完整准备时间 3507ms。第一 attempt 的 Skill
索引 1 条/1091 bytes、Memory 5 条/8302 bytes；准备与重建 16ms/1015ms。这些只作描述性观察，
不声称延迟显著改善。候选源码与最终测试期间的 SHA 核对一致。

最终清单：`test_retrieval_precision.py`、`test_skill_retrieval.py`、`test_skill_context_failures.py`、
`test_reference_orchestration.py`、`test_memory_context.py`、`test_memory_context_failures.py`、
`test_context_index.py`、`test_context_input.py`、`test_context_runtime.py`、`test_context_request_protocol.py`、
`test_skill_selection.py`、`test_history_freshness.py`、`test_product_architecture.py`、
`test_product_contract.py`、`test_retrieval_evaluation.py`、`test_retrieval_evaluation_failures.py`。
全部位于 tests，使用 `python -m pytest -o addopts= -q` 后逐个列出文件和独立临时输出目录。

## 5. 未运行的门禁与边界

检索精度整改和同冻结基线验收已完成。Release Stop C 独立审查、最终无筛选全量、L2–L4、core/TUI/
示例 Skill 的 clean-input Wheel/离线安装、真实 Provider 验收与发布均未完成。
新示例包仅从源码通过原 Entry Point loader 验证；它使用当前开发树 typed Skill SDK，
不能把已发布 0.8.0 当作已包含该 SDK 的分发证据。semantic/reranker 仍关闭。
两份上下文本轮同步 1/3/7（新增 7.9）/11/13/15/16/17/19/20 的规则、协议、结果和 Mermaid；
原 F5 的 12.5 评估口径保持，历史发布结果仍在原范围。文档链接、围栏、秘密形态与 0–20 章节对应
检查通过：16 份文档、583 个相对链接、37 个 Mermaid 块，问题 0。
