# ADR-0054：保留工具输出的关键词查找（分层压缩 B+）

状态：接受。日期：2026-09-08。

## 问题与范围

ADR-0053 B 已保存完整工具输出并提供目录、分页读取，但指定编号或关键词仍需读多页。
B+ 在 B 后、C 旧结果折叠之前补充字面搜索，不实现 C/D/E、向量检索或新的存储生命周期。

## 决定

- 默认只披露命中行，附近行需显式 context_lines 请求；这是 grep 定位与 reader 展开的职责划分。
- 默认装配增加 PURE_READ `search_tool_output`，借用同一 SessionService；输入 effect_id、digest、
  query，复用 B 的 `resolve_tool_output`。只有当前 Session 的合法 retained output 可搜索。
  该工具经过既有 Tool admission、Policy、Effect/Result、请求冻结及恢复主线；不读取工作区文件，
  不调用 Shell、不重跑原工具，不新增数据库、可变索引或批准 Memory。
- 搜索范围是一份已定位输出。引用不可见时先 `list_tool_outputs`；`part=content` 搜正文，
  `part=data` 搜同一份 data 的 canonical JSON。小 inline 输出不进入这个目录。
- query 是非空字面字符串，正则元字符没有特殊意义。case_sensitive 默认 true；false 使用
  Python Unicode 正则的忽略大小写语义，但 query 仍转义为字面量。不做语义召回或 Unicode 规范化。
- offset 为从原文起点计算的 Unicode code point，默认 0。count 默认 10、范围 1–100；
  context_lines 默认 0、范围 0–20。每项提供 match_start/end（左闭右开）、LF 分隔的一基行号、
  text_offset、before_context/matched_lines/after_context、read_action 和 context_truncated。CRLF 按原字符保存；偏移与原 reader 完全相同。
- 命中按原文顺序且不重叠。next_offset 表示最后已返回命中的末尾，后续沿用相同来源、query/options；
  上下文重叠不导致跳过命中。null 表示当前来源后缀没有更多命中。不提供易被误用的估计总命中数。
- 完整 canonical JSON 搜索页受 max_tool_output_chars 约束。容不下全部附近行时缩小首个片段，
  保留完整命中文字并标记 context_truncated；连一项都放不下则明确预算错误。后续命中分页，
  不截坏 JSON，不对搜索页再次生成 retained 引用。read_action 携带原身份与命中行起点，巨长行也不会
  强制从整行开头重新读。
- 模型的通用说明要求关键词问题先搜索；预览 offset-zero read_action 不是搜索前置步骤。
  片段足够可直接回答，否则读附近原文。搜索工具在当前 registry 可用时，预览给出搜索入口与字面 query 说明，不再附通用 offset=0
  的读取动作；仅有 reader 时仍提供原读取入口。这是按实际工具装配分支，不是旧协议兼容。
  命中行和前后附近行分字段呈现，附从命中行展开的完整读取参数，count 来自当前页字符预算。
  附近行可能属于别的记录，命中标题后必须核对正文归属，
  不得把上一条记录字段配给新标题。搜索页是实际历史文本，不是等待 Context 披露的 receipt。

## 事实源、生命周期与边界

唯一原文仍是原 effect/outcome.retained_output。搜索页是纯派生呈现，实际交给模型的页保存在原
Tool Result/request snapshot；重放不重新搜索最新来源。复用 B 的 Session/执行身份、digest、
Step、参数、因果和呈现校验；不增加第二个 resolver 或新的协议分支。

Session 10 / Context 9 / SQLite 2 / retained format 1 / M3 format 2 不变。无迁移、兼容或旧记录重写。
include_default_tools=False 的宿主须显式装配；Product capability grants 不自动扩大。
搜索仍先读取本 Session Effect 流并在内存扫描原文，整页有界不等于磁盘/CPU/全请求预算有界。
无命中只说明该字面词在所选 part/offset 后未出现，不能证明相关主题不存在。

## 验证

见 [B+ 验证](../validation-tool-output-search.md)。定向检查覆盖字面元字符、中文/emoji、长行、
重复命中翻页、无命中、data、原文位置读回、重启/压缩、错来源拒绝和确定性取消。
真实模型用运行时随机日志，分别验证 exit 0/7、搜索后展开、压缩重启、重复及无命中和新话题。
所有失败保留证据；只跑 owner/相邻测试，不跑全量、L2–L4 或安装门禁。
