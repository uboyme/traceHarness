# Stop C 修补 C1 验证记录

状态：C1 实现与限定验收完成；后续 C2–C5 继续执行。此记录不表示 Release Stop C 或 v0.9 发布通过。
设计见 [ADR-0049](adr/0049-current-reference-context-and-bounded-turn-retention.md)，
后续顺序见 [执行计划](plan/TRACEHARNESS_V0.9_RELEASE_STOP_C_EXECUTION.md)。

## 1. 当前范围

- Context 位于完整 Surface 和 Tool 调用／结果组之后，dispatch、retry、replay、invariant、Inspector 使用同一顺序。
- `reference_requests` 从原 Context 连续准入链推导三来源的 fresh/retained 句柄；仅实际注入的正文在本 Turn 保留。
- 每步复核 Skill selection/catalog/Lease、Memory active/project scope、History 来源与 freshness；History 保留原 request_ref。
- 三来源统一预算：新请求、保留正文、自动 History、自动 Skill/Memory。淘汰、资格中断、失败、取消、恢复或轮次结束后不复活。
- Session 7 / Context 6 / renderer v6 / policy v3 / disclosure Tool receipt 2 / History page policy v2；SQLite、M3、来源检索 receipt 仍为 2。旧 Session 1–6 拒绝。
- 提示按用户任务区分资料问答与工作区检查；不降低工具权限、猜测身份或自动修正错误参数。

## 2. 定向验证

所有集合均为具名选择；没有执行全量或 L2–L4。

| 集合 | 结果 | 主要边界 |
|---|---|---|
| Context Runtime/protocol、History Runtime/request/tool、Skill navigation、Memory Context、保留主线 | 149 passed，90.10 秒 | 原预算、请求绑定、来源、真实 SQLite 与重放 |
| Context input、History freshness、Skill retrieval/failures、Memory failures、reference orchestration、precision、Inspector | 171 passed，125.39 秒 | 相邻来源资格、时效、预算、取消、UI 位置 |
| OpenAI Provider、model retry/admission、Product architecture、future protocols | 75 passed，14.30 秒 | Request/Provider 邻接与拒绝边界 |
| 补齐 Memory 正向后专门保留测试 | 10 passed，29.98 秒 | 同轮连续读章/页、撤销、淘汰、重选、跨来源新请求优先 |
| 通用任务提示修订后的 navigation、retention、Context Runtime/protocol | 95 passed，62.20 秒 | 提示接线与公开请求主线 |
| JSON-null 说明后的 navigation/retrieval/failures | 99 passed，67.33 秒 | 字符串未使用 ID 拒绝、无提前披露、改正后成功与重放 |

集合存在重叠，不相加冒称唯一执行总数。compileall、修改范围 Ruff 通过；`pytest --collect-only`
收集 3259 项，仅收集未执行。正式版与通俗版主章节 0–20、7.1–7.10 对应；受影响文档相对链接、
围栏闭合、凭据／私有路径形态和 tracked diff whitespace 检查通过。

## 3. 反向验证

故障只施加于隔离源码副本，先证明子进程实际导入该副本；生产源未临时回退。

| 故障控制 | 公开路径的实际错误结果 |
|---|---|
| 删除逐步 carry | 5 个预期失败、3 个对照通过：第二章出现时第一章丢失；连续 History 页丢失；活跃 Memory 正文在第三步消失 |
| 允许保留所有旧申请，不检查实际准入链 | 2 个预期失败、2 个对照通过：重选后旧正文复活；被预算挤出的 History 原文重新占位 |
| 保留项优先于新请求 | 新明确请求的 Skill 正文被旧 History 挤出；共享预算测试按真实 block 结果失败 |
| 恢复前置 Context，并同步旧位置 guard | 中英文两项均在实际派发请求的消息顺序断言失败；没有利用 guard 误配或导入错误制造红灯 |

Memory 与重选测试的关键结果断言放在 Runtime 返回后读取实际请求的位置，避免 Provider 的错误包装
遮住正文结果。正确实现的专门保留集合随后通过。

## 4. 真实 Skill 网格与全部失败

冻结原四题、两个入口、三组无语义 ID，每模型 24 次。事前门槛为严格至少 22/24、任务至少 23/24，
scope/replay/invariant 违规为 0。工具权限、题目、答案、预算、max_steps=12 和原同请求 retry 均保持。

| 轮次 | 模型 | 严格 | 任务 | 答案 | 正文证据 | 达到门槛 |
|---|---|---:|---:|---:|---:|---|
| 首次冻结 | qwen-plus | 23/24 | 23/24 | 24/24 | 24/24 | 是 |
| 首次冻结 | qwen3.8-max-0902 | 17/24 | 19/24 | 24/24 | 24/24 | 否 |
| 首次冻结 | deepseek-v4-pro-0813 | 24/24 | 24/24 | 24/24 | 24/24 | 是 |
| 任务提示修订后第二轮 | qwen-plus | 24/24 | 24/24 | 24/24 | 24/24 | 是 |
| 任务提示修订后第二轮 | qwen3.8-max-0902 | 24/24 | 24/24 | 24/24 | 24/24 | 是 |
| 任务提示修订后第二轮 | deepseek-v4-pro-0813 | 22/24 | 22/24 | 23/24 | 23/24 | 否 |
| JSON-null 说明后第三轮 | qwen-plus | 24/24 | 24/24 | 24/24 | 24/24 | 是 |
| JSON-null 说明后第三轮 | qwen3.8-max-0902 | 24/24 | 24/24 | 24/24 | 24/24 | 是 |
| JSON-null 说明后第三轮 | deepseek-v4-pro-0813 | 24/24 | 24/24 | 24/24 | 24/24 | 是 |

两次完整网格之间，任务提示的隔离 qwen-max 八次探索全部严格通过，但没有计入完整网格分母。
后续 JSON-null 说明的 DeepSeek 八次探索也严格全过，但同样未计入完整网格。三轮完整网格与两次探索
共 232 次旅程、614 份 Request、615 次 Provider Attempt，614 次 exact usage 合计 2,002,286 tokens。
第二轮 qwen-plus 有一次 `provider-disconnected`，按原冻结请求 retry 成功；该失败没有 usage，未补零。
所有旅程 scope、原 Runtime replay 与 invariant 违规均为 0。

首次网格的失败为额外读取资料、额外查询工作区及一次抄错 catalog digest。任务提示修订后，
DeepSeek 两次多章旅程把未使用的 ID 填为字符串 `"null"`，重复错误至步数边界；其中一次最后读到了
正文并答对，但仍有工具失败，因此任务判定不通过。不能把“答对”替代原任务门槛。

更明确的 JSON-null 类型说明与错误反馈先经隔离探测，再接入生产、重新冻结全源码并跑第三轮完整网格。
第三轮三个模型全部达到原门槛，C1 验收完成。严格 validator 未放宽，没有字符串自动转换，未删除或替换
原失败。参数说明按协议类型生成，不含题目、模型或领域特例。新增测试首次因夹具未注册资源根失败，
补齐测试的显式资源绑定后 99 项通过；该夹具失败不是生产 Finding 或有效反向证据。

逐题结果、全部失败消息和 evidence/source/report 摘要见
[审计数据](validation-data/stop-c-c1-skill-2026-09-08.json)。指标已从原事件、冻结判定与最终答案重算，
逐文件核对源摘要及报告一致性，并核查实际 composed/dispatch 末尾参考与 Context 字节。
原始事件、SQLite 和源码副本保留在本次隔离临时证据目录；仓库文档不写私有本机路径或凭据。

## 5. 尚未运行或完成

C2 Memory/History/mixed 真实旅程、C3 本地混合检索、C4 原 Provider 协议失败调查、
C5 独立审查均未完成。C2 合成语料与生产接口夹具已准备，离线真实 Git/authority/索引/观察/关闭预检通过且零模型调用；
实际旅程 Runner、History bootstrap 和判定器仍待完成，真实候选调用尚未开始。
全量、L2–L4、Wheel／安装／发布门禁明确未运行，未 commit、push、tag 或 release。
