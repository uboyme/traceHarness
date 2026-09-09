# ADR-0061：受控主动参考搜索与原披露主线

状态：已实现；AR-B/AR-C 范围验证完成，AR-D 本轮执行收口但真实门槛 NO-GO（见[验收报告](../validation-active-retrieval.md)）。日期：2026-09-09。

## 背景与范围

当前自动 Context 检索无法由模型自行换词；原 History/Skill/Memory 读取工具要求对象已在本 Step 的
冻结请求中披露。因此不能只新增一个返回 ID 的 Tool，也不能绕过原 reader 直接读取任意账本。
本决定落实[主动检索计划](../plan/TRACEHARNESS_ACTIVE_RETRIEVAL_EXECUTION_PLAN.md)的 AR-A，
用户已授权依序执行 AR-A–AR-D。上一阶段提交为 `a54d431`，只作为基线，不推送或自动发布。

## 决定

### 1. 入口与查询

新增三个 PURE_READ Tool：`search_history`、`search_memory`、`search_skill`。它们绑定构造时借用的
同一 SessionService 和 Context policy，经原 ToolRuntime 的调用/结果/Policy/Effect 审计执行。
不新增 AgentLoop、Session、Provider、持久搜索数据库或可变 pending。

输入统一为非空 `query`，可选 `cursor`（缺省为 null）和 `limit`（缺省取当前 Context 的 max_blocks）。
query 是一个字面词或短语，不是全文问句或正则；采用 Unicode 忽略大小写的字面匹配，保持原字符偏移。
query UTF-8 bytes 不超过原 max_query_bytes；limit 为正整数且不超过原 max_blocks。
不同来源仍由各自 reader 解释业务状态，不引入万能业务投影或主题同义词表。

cursor 包含 `source_digest`、`query_digest`、`policy_digest` 和 `offset`，offset 是稳定来源对象/记录
顺序中的下一扫描位置，不是任意 Session seq 或原文页 cursor。改变 query、策略或绑定来源不能复用旧 cursor。
无命中页也可带后续 cursor，明确说明已搜索/未搜索范围，不声称整个主题不存在。

### 2. 来源范围与排序

- History：原 HistorySnapshot 可解析的当前可见压缩根，递归展开仍复用原来源验证和完整 Turn 分页逻辑。
  在已验证原消息文本上查找，按根与原消息位置稳定排列，重复来源不重复计分；不搜索合成 Context、摘要请求、
  原 Product 控制流或别的 Session。来源绑定为压缩根和 leaf refs，不因无关追加事件失效。
- Memory：原 MemoryContextReader 返回的当前项目 active 批准短事实及其 ID/fact_slot 元数据，按稳定身份排列。
  搜索前后核对项目绑定和来源 heads；不搜索提议、撤销、被替代事实或跨会话批准依据。
- Skill：原 eligible_skills 给出的宿主选择与当前 Composition 目录；搜索顶层、section、resource、chunk
  的标题、summary 和稳定 ID，按目录身份顺序排列。不加载正文来建立索引，不发现/启用未选插件。
  资源卡若包含多个块，只返回本页实际匹配的有界导航，不展开整份大目录。

三者都覆盖当前授权集合中的自动 Top-K 外对象。排序采用确定性字面定位，不改原自动 exact/FTS/RRF 规则，
不启用语义或 reranker。来源为空、未选择、未绑定、stale 与零命中分开表达。

### 3. 搜索结果与正文授权

增加内部 Context block `tier=search`，kind 仍是 history/skill/memory。它表示一份派生搜索页，
不作为新的业务事实种类，不开放为原披露 Tool 的 requested_tier。
标准 block 外壳保留 kind/id/version/scope/source_refs/body/content_digest/content_bytes/provenance；
搜索页身份由来源、查询、策略、offset/limit 和返回内容派生，不能冒充命中对象本身的 ID。

body 为 canonical JSON：查询、覆盖字段、状态、实际扫描量、hits、next_cursor。每个 hit 有稳定来源身份、
原文/元数据短片段、片段位置与裁剪说明、以及精确 read_action；Memory hit 保留批准对象来源身份，
Skill hit 带 exact id/version/catalog/section 或 chunk，History hit 带原 root/version/page cursor 和 leaf 位置。
只有对应 reader 确实能接受的动作才非 null。大片段只提供有界引用，不冒充完整 Memory 或完整 Tool reply。

Tool Result 只返回成功搜索申请的回执：format=1、session/turn/source_step/tool_call 身份、原 context_ref、
规范请求、policy_digest、source_digest，target_rule=`next-step-only`。不放命中片段或参考正文。
下一 Step 的 Context 从成功回执、原 Tool call/assistant/request 身份与冻结来源重建搜索页，再走原预算准入。
无效或失败/取消回执不产生候选；来源变化形成明确失效结果，不静默换成新来源继续旧分页。

搜索页只供紧邻下一普通 Step，不保留至整 Turn；原阅读工具只能从**本 Step 已准入搜索页**提取合法命中句柄。
读取仍重新核对实际 scope/active/selection/catalog，不能仅相信页中字段。搜索页未准入时不会产生正文授权。
正文被正式请求后继续走原 fresh → bounded retained → 退出的唯一规则；过期搜索页不是长期授权。
同一步多工具调用不能消费尚未注入的搜索页；模型应先取得当前搜索结果，再发起需要该结果的读取。

### 4. 预算与原子性

复用现有 Context policy 的明确边界，不另加一组默认 JSON 参数：搜索页和普通引用共享 total_bytes、item_bytes、
max_blocks、kind bytes 与完整请求 token 准入；query 共用 max_query_bytes；每 Step 每来源搜索调用数共用该
来源 max_requests。搜索 limit 的默认及上界均由当前配置 max_blocks 派生，无案例/平台/模型隐藏值。

源构建继续受 History max_source_events/max_source_bytes/max_depth、Memory authority 资源上限和
ReferenceRetrievalPolicy max_corpus_items/max_corpus_bytes、Skill max_catalog_bytes 等原 owner 限制。
单次扫描在这些有界来源内执行；索引查询和输出短小不能被宣称为无界来源的低成本保证。
片段按最终页预算缩短并标明裁剪；一条完整命中身份/动作也放不下时明确 resource-limit，不截坏 JSON。

首次新正文请求优先，其后为新搜索页，再为已实际保留正文、自动 History、自动 Skill/Memory。
这一顺序在原 `_admit_candidates` 一处表达，token 和字节模式一致，来源重建重算相同顺序。
只在当前候选中临时派生优先级，不增加第二预算账或会话级搜索状态。

History 的原文页继续由完整 Turn 组成，活动 Tool 调用/结果原子性不变。搜索片段是引用文字，
不能当作新的 assistant/tool 消息插入 Surface；超大页保留明确不可读状态。
如果原 retained output 读取入口可验证可用，沿现有输出搜索/阅读工具处理；不重跑原命令。

### 5. 装配、生命周期与协议

普通默认 Runtime 在同一来源读取已启用时装配其搜索 Tool；include_default_tools=False 时仍由宿主显式装配。
Memory 必须有真实 authority 与绑定读取器，Skill 必须有原目录/选择 owner；功能关闭时不出现虚假搜索能力。
CLI/TUI 复用现有 History/Skill/Memory 功能开关及预算表单，不新增用户需要填写的身份字段。
Product requester 的原只读白名单/能力约束不扩大；可以因其既有 Policy 拒绝新工具，并如实表达不可用。
插件重组仍从原配置重建同一来源工具；旧 Step Lease 不引用新代资源，新 Step 不接受旧目录句柄。

取消与释放沿原 Runtime/ToolRuntime/Session/Lease 收敛，搜索只是有界纯读取，不启动游离线程或后台模型。
校验来源变化的测试使用明确 Gate，不用 sleep 猜测。过去已经披露的文字不能被抹除，失效约束针对后续准入。

Context format 10、renderer `context-json-v10`、Session context_protocol 11 为本次唯一新合同；
Context policy 字段仍为原十二项，policy version 更新为 `active-reference-policy-v6` 以绑定新增查询语义。
普通披露 receipt 仍为 2，SQLite/M3/retained output 格式不变。旧 Session 1–10 明确拒绝且不改写、不迁移、不删除。
新数据目录/新 Session 要求必须在 CLI/TUI 和正式/通俗文档说明；本轮不修改用户原会话或启动 Profile。

请求重建以原 Context block、原查询回执、当时来源前缀/目录与冻结策略核对。新 tier 必须进入既有 parser、
source validator、SessionService、invariant、replay、预算、Inspector/TUI，不允许仅 renderer 接入或跳过校验。
搜索页证据来自原账；索引不是权威，旧请求不能用最新 live 状态重新生成。

### 6. 冻结字段与拒绝面

所有新对象严格拒绝额外键；Tool 参数允许省略的字段只在原参数解析入口规范化一次，审计仍保留真实输入。

| 对象 | 字段 |
|---|---|
| 规范查询 | `query`, `cursor`, `limit` |
| 搜索 cursor | `source_digest`, `query_digest`, `policy_digest`, `offset` |
| 搜索回执 | `format`, `session_id`, `turn_id`, `source_step_id`, `tool_call_id`, `context_ref`, `request`, `policy_digest`, `source_digest`, `target_rule` |
| search block provenance | `request_ref`（成功 Tool result 引用）, `policy_digest`, `source_digest` |
| search body | `query`, `fields`, `status`, `scanned`, `total`, `hits`, `next_cursor` |
| hit | `reference`（原对象/来源的可验证导航身份）, `text`, `text_offset`, `match_start`, `match_end`, `truncated`, `read_action` |

`reference` 为来源各自的严格结构，不接受混合身份：History 绑定 root/version/leaf ref/page cursor；
Skill 绑定 descriptor/catalog/section/resource/chunk；Memory 绑定原 scope、ID/version、fact_slot、批准引用。
嵌套具体结构复用这些 owner 的现有对象，不新增无约束任意 metadata。read_action 沿现有 tool_name/arguments 格式。
偏移按原被搜索文本的 Unicode code point 计数，match 为左闭右开；snippet 不伪装为完整页或完整批准事实。
total 仅为当前有界、完整构建来源中的可搜索记录数，不是命中总数；来源不能完整构建时明确资源错误，不编造 total。

status 限定为 `matches`、`no-hit`、`source-unavailable`、`resource-limit`；未启用/未绑定/未选/stale 的原因在
原 Tool 稳定错误或 Context exclusion 中区分。只有 matches 中通过验证的 read_action 能用于后续授权。
source-unavailable/resource-limit 页的 hits 必须为空、next_cursor 为 null，扫描/数量字段不得伪造完成覆盖。
当页还有记录未扫完时 next_cursor 非 null；扫描完成但无命中时为 no-hit，不等同于相关主题不存在。

## 实施与验证

AR-B 先落 History 和共同 tier/receipt 接缝，AR-C 再复用该机制补 Memory/Skill，不在未实现来源装配空壳 Tool。
AR-D 用[冻结评测清单](../../tests/live_active_retrieval/manifest.json)执行原基线与候选的自然问答。
新测试至少覆盖正常、错误身份/预算、失败/取消；关键保护有反向证据。历史页完整性、引用退出、重启、
项目绑定变化、Memory 撤销/替代、Skill 退役、Provider 重试和当前问题保护为相邻门禁。
没有越权披露、重复副作用、无证据编造与旧批准事实冒充当前事实；真实正确/证据率分别按冻结门槛核对。
只跑 owner/相邻、必要真实验证和文档 QA，不运行全量 pytest、L2–L4、安装/Wheel 或发布门禁。

本 ADR 不宣称实现或验收已完成。实现与每次失败证据随阶段链接回执行计划，不修改已冻结旧语义评测来证明收益。
