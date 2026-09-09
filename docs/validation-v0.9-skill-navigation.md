# v0.9 F5 Skill 导航与真实模型验证

本轮范围为 Skill 导航合同、真实模型的目录理解与正文披露，以及真实测试发现的数字表示/标点问题。
不是 Release Stop C 或发布验收；未运行全量测试、L2–L4、Wheel、安装、提交或推送。

## 1. 实现

- section/resource/chunk 必填标题和说明；原 catalog/digest/revision 冻结，大小使用原宿主上限。
- 目录与回执共用导航卡；正文只在原 immediate-next-step Context 注入。
- Skill notice、工具描述/回执和宿主固定提示解释“目录是什么、如何选章、正文在哪里”。
  live/frozen PromptAssembler 共用一处装配规则，参考内容仍不是 system 指令或权限。
- 保留 JSON 温度的整数/浮点表示，避免 `0 → 0.0` 破坏请求绑定。
- 处理自然语言冒号，保留显式引用和复合代码标识的整体匹配。
- Session 6 / Context 5 / renderer v5 / tokenizer v3；旧 Session 1–5 拒绝。
  SQLite/M3 2、policy v2、来源 receipt 2、ranker v2、配置结构不变。

设计依据：[ADR-0048](adr/0048-skill-navigation-and-real-provider-disclosure.md)。

## 2. 程序验证

25 个具名文件：548 passed，1 skipped，132.04 秒。包含 Skill 六个原 owner、新增导航、精度、
统一引用、Context/Request/Index、Memory Context、History 请求/工具/Runtime、Composition、
模型重试、OpenAI-compatible adapter、Runtime factory、Product 架构/合同和 CLI 配置。
最终源码补充 6 文件为 **198 passed / 1 skipped in 35.34s**，与前者重叠，不相加：导航、Composition
generations/overlays、Skill contributions/resources、Context protocol。唯一 skip 为
`test_skill_resources.py` 的 Windows symlink privilege。冻结检索/失败取消 2 文件最终 **18 passed in 882.00s**。与最初 25 文件不重叠。
另对 prompt 直接相邻入口补跑 `test_scope_overlays.py`、`test_plugin_runtime.py`、
`test_plugin_extended_contributions.py`，**70 passed in 3.62s**。合计 30 个不同文件 **636 passed / 1 skipped**；
补充 198 项不重复计数。

`python -m compileall -q src tests`、本轮 20 个 Python 文件 Ruff、反样本硬编码扫描和 `git diff --check`
通过；`pytest --collect-only` 为 **3244 tests collected**，只收集、未执行整仓。两份上下文 0–20 主章节和
7.1–7.10 子章节对应，Markdown 相对链接存在、代码块闭合；导航图按实际 owner 和消息位置核对。

最初 25 文件的命名清单如下，均通过显式文件选择执行，没有使用整仓执行或缓存失败选择：

```text
test_skill_navigation.py, test_retrieval_precision.py, test_skill_contributions.py,
test_skill_resources.py, test_skill_selection.py, test_skill_retrieval.py,
test_skill_context_failures.py, test_reference_orchestration.py, test_context_input.py,
test_context_request_protocol.py, test_context_index.py, test_context_runtime.py,
test_memory_context.py, test_memory_context_failures.py, test_history_requests.py,
test_history_tool.py, test_history_runtime.py, test_composition_generations.py,
test_composition_scope_overlays.py, test_model_retry.py, test_openai_provider.py,
test_runtime_factory.py, test_product_architecture.py, test_product_contract.py,
test_cli_context_config.py
```

冻结基准另选 `test_retrieval_evaluation.py` 与 `test_retrieval_evaluation_failures.py`。所有上述文件
位于 `tests/`；补充 6 文件在原清单中。执行使用 `python -m pytest -o addopts= -q` 加具名文件和独立
临时输出目录；没有选择 evolution/候选验证或自动联网的长门禁。

新导航模块 44 项，精度模块 25 项；与上面清单重叠，不把次数相加冒充用例数。
四组反向证据：缺导航时公开 Runtime 无法按目录选章（1 项失败）；旧数字转换使整数温度的
section/chunk 绑定失败（2 项失败，4 项浮点对照通过）；旧冒号规则导致三类自然语言漏召回
（3 项失败）；只改 live PromptAssembler、未接入 frozen 主线时，实际请求缺少宿主说明（1 项失败）。

## 3. 真实调用方法

输入见 [真实集成测试](../tests/live_skill_navigation/README.md)：四类自然语言任务，目录/摘要两种
入口，每个正式网格三组不同无语义 ID。正确章节和答案只保存在测试判定资料中；模型调用均由真实
Provider 生成。三个已激活 Skill 中只选择两个，第三个用于确认未选中材料不进入模型请求。

每次使用独立的空工作区和 SQLite；可信测试插件通过标准库 Entry Point 元数据和原激活链装配。
资源在工作区之外，宿主只允许引用工具，文件/进程调用拒绝并计为失败。全过程保存冻结请求、原事件、
调用 ID、实际披露、答案、重放/不变量、来源摘要及可用 token 计量。

严格导航 `passed` 要求答对、取得全部需要的正文、没有多余正文读取、没有非法/失败工具调用，且
重放和不变量通过。`task_passed` 另报取得证据并完成任务，允许额外读取；严格失败不改名为严格成功。
Provider 错误单列，不并入模型选章错误；补测也不覆盖原失败。

## 4. 真实结果

以下每个模型均为同一最终生产源码、同一合成语料、4 题 × 2 入口 × 3 组不同 ID，共 24 个样本。
temperature 为整数 0；每样本最多 12 个 Step、单响应最多 16384 output tokens，Provider timeout 300 秒，
使用原 bounded retry，不切换模型。表内保留失败分母，不把“答对”直接当成完整通过。

| 模型 | 答案包含所需事实 | 有证据的任务完成 | 严格选章通过 | 目录入口严格通过 | 摘要入口严格通过 |
|---|---:|---:|---:|---:|---:|
| `qwen-plus`（现有配置） | 13/24 | 11/24 | 11/24 | 9/12 | 2/12 |
| `qwen3.8-max-0902` | 24/24 | 20/24 | 17/24 | 11/12 | 6/12 |
| `deepseek-v4-pro-0813` | 20/24 | 20/24 | 20/24 | 12/12 | 8/12 |

`qwen-plus` 已有 16/24 取得所需正文，但仍会漏读当前 Step、提前结束或调用被拒绝的工作区工具。
`qwen3.8-max-0902` 全部取得所需正文并答出事实，其中 3 个样本多读了伴随章节，另 4 个样本额外调用
`list_files` 并被 Policy 拒绝；这些样本分别保留严格失败或任务失败。`deepseek-v4-pro-0813` 有 3 个
摘要入口在收到目录后提前结束，另 1 个样本 Provider 返回不合协议的工具参数：SQLite 原事件记录
`failure_category=protocol` / `provider-tool-arguments-invalid`。它不是网络失败，未被删除或补测覆盖。

同一 `qwen3.8-max-0902` 的导航移除对照 **0/24 答对、0/24 取得所需证据、0/24 严格通过**。
对照使用最终源码独立副本，仅 `api/skills.py` 的 directory/available 导航投影退回旧 ID 形式，其余
源码、模型、语料、无语义 ID、Policy 和调用上限一致。这是导航元数据消融，不冒充完整旧版本复验。

正式四网格共 **96 个场景、370 次真实 Provider Attempt**。369 次有 exact usage，合计
**1,070,295 tokens**；另 1 次协议失败用量 unavailable，不记成零费用。开发中的诊断调用未计入这组
正式数字，所有早期失败另行保留。95 个正常返回场景的请求重建、不变量与选择隔离全部通过；1 个
Provider 抛错场景保留 SQLite 和失败事件，该场景的整轮重建检查未执行，不用默认空列表冒充通过。

逐样本指标、原报告/结果/事件摘要、模型与调用界限见
[可核对的结果数据](validation-data/skill-navigation-2026-09-08.json)。所有正常网格的生产源码摘要与
交付时一致；对照只差 `api/skills.py`。最终运行开始时脚本有归档且摘要吻合；随后测试脚本仅补充
异常时导出 partial events 和稳定 Provider code/category，不改请求行为或判定。原 Provider 失败的
分类从原 SQLite 只读恢复，原失败报告未覆盖。

开发阶段的真实失败记录全部保留：第一轮发现整数温度绑定错误；随后发现中文冒号漏召回和模型
把 Skill 当成工作区文件的问题；固定提示曾遗漏 frozen 装配分支，已由反向用例定位并接回共同 owner。
现有 `qwen-plus` 配置的摘要入口与顺序跨章阅读仍表现不稳定，不能用其他模型的成功冒充其成功。

因此本轮证明了新导航能被真实模型使用，也发现当前默认模型尚不能按稳定可用验收。代码回归绿色
和真实语义验收全部绿色是不同结论；本轮没有通过 Release Stop C，也没有把模型失败藏成成功。

## 5. 冻结检索协议重新绑定

第一次运行原 11-query 基准正确拒绝 `retrieval-seed-catalog-mismatch`：新增章节 title/summary 改变了
真实目录身份。原 2 文件回归为 17 passed / 1 failed；保留该负向证据，不声称它是既有基线问题。
移除新字段后精确重算出原 catalog digest，证明变化来自本次描述符协议。

仅修改 `corpus.json` 的 `plugins.catalog_digest` 和 manifest 依赖的文件摘要，字节级比较确认其他
语料字段、query、judgment、质量阈值、预算和初始工作区未变。没有放宽 parser 或跳过绑定检查。

| 摘要 | 原值 | 当前值 |
|---|---|---|
| Skill catalog | `1b9d825b71d99aba2376b0eb605c1dd7cb2a097ed451e8215329138169151ef5` | `4a37d1256a7ae2ea2b2bea7a576c793744c6f730d9dba55cc23023b5a25b946b` |
| corpus SHA-256 | `a1822e75c1a3caa1d8b52f7799e1f41218c7a50d17fb29ce8ae386da4c3f714e` | `eef440b9401360cca66aa195403ca1e261acbd56d492f4ba702870673da260f3` |

最终 Product **11/11**、原阈值 quality_passed **11/11**、44 个 Step、隔离违规 **0**。语义题仍只达到原词法底线 0，不表示语义能力达标。
报告 SHA-256：`bc30d2a5cf59ddf9d570033213d78dedca4879de0f1e3ee49955b372088812cd`。
该基准仍通过确定性 Provider 检查原 Product 11-query 合同；上节真实模型导航由另一个显式集成测试
检查，不能混称为“真实模型完成 11 条 Product 基准”。

## 6. 已知边界

目录说明由贡献者维护，系统不从正文自动提取或生成。当前仍是已选 Skill 顶层元数据的词法召回，
不搜索未选中 Skill，不具备全文/语义召回。大目录按原预算整块排除，尚无目录分页。
实际模型可能忽略说明、多读或没有记住上一 Step 的内容；有限网格不证明任意模型或任务都可靠。
真实调用使用现有服务的显式模型选择，不更改用户 `.env` 或默认模型，不使用静默 fallback。
