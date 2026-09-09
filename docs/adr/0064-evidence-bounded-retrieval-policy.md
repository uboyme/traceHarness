# ADR-0064：显式证据导航与读取覆盖范围

日期：2026-09-09。状态：采用呈现合同；否决长提示重写、后置提醒与默认强制自评。真实证据见[记录 006](../deal/006-evidence-bounded-retrieval-policy.md)。

## 问题

真实模型会把 Skill 主题、目录未命中或工具输出的第一页当作整个来源，提前回答“不存在”。模型也会说“继续搜索”却不发起工具调用。原系统已经支持搜索与读取；拥有能力、实际调用、读取范围和语义正确是不同的验收对象。

相同问句只改变隐藏正文的正反事实对照暴露了这一问题。重写约 900 词的系统指导、把检查提醒放在最后、通过原 Continuation 增加一次草稿复核，都没有稳定改善。没有采用这些候选，也没有为模型自评增加生产状态或新的证据账本。

## 决定

修复负责展示的现有层，让来源性质和读取范围成为明确数据：

1. Context 中 Skill summary/directory 标为 `navigation-only`，section/chunk 标为 `source-content`。summary 带由当前 block 的原身份生成的目录读取动作，unused ID 为 JSON null；目录内章节仍由模型选择，正文仍走原 Reader 与下一 Step 披露。
2. 大 Tool 输出在当前组合暴露搜索或读取工具时，先给 `output_ref` 和查找入口，不先展示容易被误作全文的前缀。两种工具均未装配时保留明确不完整的预览；小结果保持 inline。这是依据实际组合的呈现选择，不新增配置或权限。
3. 读取页返回 `end_offset` 和 `body_status`，范围始终是所选 `part`（content 或 canonical data）。只有 offset=0 且该页读到该部分末尾才是 `complete-source`，其他情况均为 `source-excerpt`；从中间读到 EOF 仍不是全文，也不代表读过另一部分。搜索返回 `match_mode=literal-substring`，说明它只能证明相应原文范围内的字面命中。

这些字段与其 JSON 开销一起进入既有页/Context 预算，没有扩大上限。位置按原 Unicode code point 计算，片段仍可通过原句柄继续读。完整原文、结构化 data 和证据仍只保存在同一 Effect outcome；恢复补齐同一呈现，不重新执行命令。

## 所有权与版本

来源及有效状态：原 Reader/Projection、Skill selection/Lease、Memory authority、Session/Effect。
呈现及准入：原 Context renderer 与 Tool output owner。
调用权限：原 ToolRuntime 每次检查。目录动作和工具暴露都不授权。
结束：原 Continuation、预算、取消、重复拒绝与可选 Verifier；没有自然语言判断器或第二状态机。

Skill 搜索仍只覆盖所选文档的目录字段，没有新增全文索引、向量检索、固定同义词或跨来源搜索。检索片段足够时可以回答，不强制为次数读更多内容。

当前唯一协议为 Session 13 / Context 12 / active-reference-policy-v8 / context-json-v12。旧 Session 1–12 明确拒绝并保留，使用新数据空间；不兼容解释、迁移或删除。SQLite/M3 和 format-1 output_ref 未变。旧实验仅按各自冻结源码独立重放，包括未采用的 Context 试验，不能交给当前 renderer 重新解释。

## 验证和仍有的限制

公开 Runtime/Tool 路径验证导航动作、精确身份、原文读回、完整/片段范围、组合差异、失败、取消、恢复和重放。反向恢复旧呈现，测试确实在已发生的请求或结果上失败，恢复后通过。

真实最终导航对照有答案题为 7/9→8/9；原有负例仍会因手册主题过早作否定。最后覆盖标记对照的候选为 5/5，基线 4/5 中唯一未完成为 TLS EOF，不能把该差值归因于修复。`completed` 不证明证据充分，也不保证模型理解这些字段。本决策改善可观察的呈现合同，不宣称解决了所有 Agent 检索策略问题。
