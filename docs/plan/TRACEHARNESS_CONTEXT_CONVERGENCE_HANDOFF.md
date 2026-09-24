# 交接：长任务上下文治理与 multi 收敛

**给接手的 Coding Agent。** 先读 `AGENTS.md`，再读本文件，再按第 2 节按需展开。
本文件只陈述可核查的事实与明确未做的事，不复制合同正文。

**当前续接范围（2026-09-22）**：用户已将后续目标收拢为上下文治理、Single/Multi 和有界策略优化，
并进一步授权目标模式完成实验、必要修复与简历更新，见[三项目标计划](TRACEHARNESS_AGENT_EFFECTIVENESS_PLAN.md)。
新增 API 总预算最多 120 元，包含诊断、试次、策略搜索与审阅；完整集成检查和材料准入已启动。
本文件保留历史证据与门禁欠账，后续按新计划逐轮根因分析和明确授权推进，不自动实施 085 的任一提案。

**最新停止点**：修复 Single 交付预留接线后，已有一次完整成功及同题失败的 Multi 对照；
首组上下文对照完成但双臂都未通过。原生有界优化双臂触发参数 JSON protocol 失败与未知用量，
新增付费已停止，九题内部验证未执行。当前结果见[证据汇总](../validation-data/agent-effectiveness-v1/RESULTS.md)，
失败分类的最小方案见[待批准决策](TRACEHARNESS_PROVIDER_SAMPLING_DECISION.md)。全量最终绿色门禁仍未完成。

## 0. 三十秒摘要

- 方案 [080](../deal/080-long-task-context-and-completion-design.md) 的 **C0–C4 已全部实施**，
  另有真实运行逼出的 **9 项后续修复**，每项都做了会失败的反向验证。
- **已记录的上下文越限得到控制**：run4 之后再未出现单次请求越限，峰值全部低于 61,184；完整任务收益仍未证明。
- **历史 C4 的 14 轮真实运行中固定 Verifier 一次也没跑到**；新授权实验已经取得真实裁决，见页首。
- 工作区**未提交、未推送**。新授权下首个完整全量已跑，命中原 16 失败 + 17 ERROR；修复与最终绿色门禁见[记录 086](../deal/086-agent-effectiveness-overnight.md)。
- 下一步的三项提案在 [085 §7](../deal/085-multi-agent-convergence-diagnosis.md)，均未实施。

## 1. 当前工作区状态

改动集中在这些文件（`git status --short` 可见全量，其中包含与本工作无关的用户既有改动）：

| 文件 | 改了什么 |
|---|---|
| `src/traceh/api/llm.py` | `CompletionCategory`、`Usage.reasoning_tokens` |
| `src/traceh/llm/openai_compatible.py` | finish_reason 映射、推理分项读取 |
| `src/traceh/runtime/response_completeness.py` | 新增：响应完整性判定 |
| `src/traceh/runtime/agent_loop.py` | 执行工具与 Verifier 前先判完整性（受保护文件） |
| `src/traceh/session/tool_output.py` | `output_ref` format 2 + `disclosure` |
| `src/traceh/session/surface_replacement.py` | 紧凑占位符、`fold_source_call`、需求加权读回保留 |
| `src/traceh/session/compaction.py` | 闭合 Step 切口、fold boundary 参数 |
| `src/traceh/runtime/request_builder.py` | 软硬水位、准入路径折叠失败必须停止 |
| `src/traceh/product/collaboration.py` | 每步停止 `_stop_on_undeliverable_report`、主方收尾 |
| `src/traceh/supervision/investigation_wrap_up.py` | 新增：有限收尾预留 |
| `src/traceh/budgets/enforcement.py` | 重试共用一份不可观测扣额 |
| `src/traceh/tools/runtime.py` | 零工具 Step 的拒绝回执 |
| `src/traceh/cli/main.py` | 新增 `--model-timeout-seconds` |
| `src/traceh/product/runtime.py`、`src/traceh/product/config.py`、`src/traceh/api/product.py` | `context_policy` / `wrap_up_reserve` 装配 |
| `src/traceh/evaluation/evaluators/retrieval_episode.py` | 评测来源口径按 `disclosure` 筛选 |

新增测试：`tests/test_response_completeness.py`、`tests/test_output_reference_eligibility.py`、
`tests/test_in_turn_step_folding.py`、`tests/test_investigation_wrap_up.py`、
`tests/provider_response_probe/probe.py`、`tests/real_repository_evaluation/c4_context_trial.py`。

## 2. 按需阅读

| 想了解 | 读 |
|---|---|
| 原始设计与为什么 | [080](../deal/080-long-task-context-and-completion-design.md) |
| C0–C3 实施与 C0-2 探测 | [081](../deal/081-long-task-context-and-completion-implementation.md) |
| 第一次失败的独立日志诊断 | [082](../deal/082-c4c-context-and-late-stop-diagnosis.md) |
| 四个开源实现的对照调研 | [083](../deal/083-open-source-agent-context-research.md) |
| **九项修复 + 14 轮运行序列 + 验证边界** | **[084](../deal/084-fold-maturity-fixes.md)** |
| **multi 为什么慢 + 三项提案** | **[085](../deal/085-multi-agent-convergence-diagnosis.md)** |
| 模块合同 | `project-context.md` §8.4 / §9 / §12.2 / §14.3.19 |
| 同一事实的通俗解释 | `project-context-plain-zh.md` 对应主题 |
| 试次冻结条件 | `docs/validation-data/real-repository-pilot-v1/c4-preregistration.json` |

## 3. 已验证的事实（有证据路径）

证据都在 `docs/validation-data/real-repository-pilot-v1/c4-context-governance-trial-*.json`
与 `.traceh/c4*-run/attempts/*/ev/events.sqlite3`（只读打开）。

- **上下文越限自 run4 起未再出现**，峰值输入全部低于硬上限 61,184。
- **每步停止**在真实运行中确认：`c4g-run` 最后一次 collect 之后模型调用增量为 **0**，
  1.9 秒后记录 `collaboration-child-terminal-failure`；对照 run3 的 562,727 token / 18.52 分钟。
- **有限收尾触发过**：`c4h-run`、`c4m-run`、`c4p-run` 出现 `investigation-wrap-up` 视图。
- **助手唯一一次成功交付**：`c4e-run`，`completed`，12,732 字报告。
- **重试共用扣额**在真实主线确认：`c4n-run` 中 ordinal ≥2 共 3 次结算合计 **0** token。
- **读回深度循环消失**：同一页最大重开次数 4 → 2（`c4g-run` → `c4h-run`）。

## 4. 门禁状态（**必读**）

**已执行**：受影响 owner 与相邻 owner 的定向测试全绿；每项修复各自做了会失败的反向验证；
`python -m compileall -q src tests`；`pytest --collect-only`；修改范围 Ruff；
`git diff --check`；文档链接 / mermaid / 秘密扫描。

**2026-09-22 新授权下的集成状态**：完整 `python -m pytest -q --durations=30` 已运行，
原 16 失败 + 17 安装 ERROR 逐项一致。已修复当前解释器元数据、示例插件兼容范围、旧夹具的 UTF-8、
配额口径与 Sandbox 装配；真实 L2 使用测试自己拥有的当前源码不可变仓库，不提交主仓库。
第二次全量因运行中源码修改触发冻结漂移，不能作为最终门禁；源码稳定后仍需完整绿色确认。
安装、Wheel、真实隔离和 L2 已随本次跨域集成纳入；L3 无 Sandbox 的失败关闭边界单独披露，L4 未跑。
详细命令、失败与结果统一见[记录 086](../deal/086-agent-effectiveness-overnight.md)。

**既有基线失败**（改动前即存在，已在干净 HEAD 独立复现，与本工作无关）：
`test_candidate_comparison.py`、`test_evaluation_workers.py`、`test_active_retrieval_grid.py`、
`test_candidate_validation.py`、`test_episode_review_contract.py`、`test_evaluation_comparison.py`、
`test_evaluation_comparison_evidence.py`、`test_manual_optimization.py`、`test_memory_product.py`、
`test_reference_orchestration.py`、`test_retrieval_precision.py`、`test_version_contract.py`
共 16 项，另有 `test_plugin_wheel_e2e.py` 17 项 ERROR（安装门禁）。
**不得把这些当成本轮引入，也不得当成可以红灯交付的理由。**

## 5. 历史待决策与新授权进展

以下仍是候选清单；当前优先级与启用条件由[三项目标计划](TRACEHARNESS_AGENT_EFFECTIVENESS_PLAN.md)维护，
不能把历史排序当作实施授权。先取得有判别力的单题证据，再决定具体路线。

1. **畸形采样被归为永久失败**（[084 §10](../deal/084-fold-maturity-fixes.md)）。
   `provider-tool-arguments-*` 属 `protocol` 类，该类不可重试；
   但那是模型采样内容坏了，不是供应商协议坏了。543 次调用中出现 1 次，杀掉过一整轮。
   `failure_category` 落盘，改它是持久协议变更，按 AGENTS §8.1 应作为独立架构决策。
2. **[085 §7](../deal/085-multi-agent-convergence-diagnosis.md) 的三项提案**：
   增量交付、并发模式长等待仍未实施；换模型已作为新授权实验的显式冻结条件，不是改生产默认。
3. **single 模式同题运行**：14 轮 multi 一次未达 Verifier。
   新授权下已执行，第三轮在收尾修复后完整通过原固定验收；模式收益须等待同条件 Multi，不能由 Single 成功推断。

## 6. 两次我自己的误判（留作证据，不要重蹈）

- **永久豁免读回结果**：为堵"取回又被折走"，一度把读回结果永久排除出折叠候选。
  这是过度纠正：28 条永久保留占了那次被拒请求的 42.5%，只是把失败搬了个地方。
  已撤销，改为按字节容量 + 需求加权（084 §4、§6）。
- **在记账修好之前放宽重试**：把重试从 3 次放宽到 5 次，使每次网络抖动的扣额从约 20 万涨到约 50 万，
  直接导致 `c4k-run` 的额度耗尽。记账修好之后放宽才是安全的（084 §7.2）。

两者的共同教训：**修复要回答"删掉这段保护，哪一条当前用户可见合同会被破坏"。**
答不出来时，多半是在把问题搬家。

## 7. 硬边界

- 未授权不得 commit / push / tag / release。
- 不得读取、打印或提交真实 `.env`、API Key、Token。
- 真实 Provider 调用要花钱；`.traceh/c4*-run` 是已花费的证据，**只读**，不得覆盖或删除。
- `c4q-run` 是**中途人为终止**的运行，没有产出证据 JSON，不能当结果读。
- 预登记 `c4-preregistration.json` 中的停止条件仍然有效；
  已披露的条件变更（输出上限、`context_policy`、读回工具授权、重试窗口、请求超时）
  使本试次**不是**对 v10 的受控对照，不能据此声称节省率或同等质量。
