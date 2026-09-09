# ADR-0055：旧工具结果折叠（分层压缩 C）

状态：接受。日期：2026-09-09。

## 范围与问题

B/B+ 已保存大工具结果并提供目录、搜索和读回，但旧预览仍长期占据 Surface。
C 只降低已结束旧结果的呈现成本，不实现 D 模型摘要或 E 轮内完整请求预算。

## 决定

- 复用 CompactionService 的 Turn 前维护入口、Surface replacement、CAS 写入与提交对账。
  自动压缩显式开启且可见对话 canonical UTF-8 bytes 达到原 trigger 时，先从最旧的合格
  retained Tool result 开始逐条折叠；每次重读重新计量，低于 trigger 即停止。
  仍超阈值且没有可折叠结果时，才走既有 M3 闭合历史前缀摘要。
- 最近 keep_recent_turns 个闭合 Turn、未闭合 Turn、Product 状态均不作为折叠来源。
  原 assistant Tool call、名称、参数、顺序保持不变；只把一个旧 Tool reply 的正文变短，
  role=tool、name、tool_call_id 均保留。多调用组中短结果保持原样，不把工具配对改成 prose。
- 仅折叠原 tool/result 中带 output_ref、目前仍可见且确实节省字节的结果。
  写入前用 B 的 resolve_tool_output 校验同一 Session/Effect/调用/结果/因果/参数与 digest。
  小 inline 结果不参与。折叠内容由宿主确定生成：历史说明、invocation_status、原 output_ref。
  invocation_status 不是进程 exit code；需要细节时沿原搜索/读取主线查证，不能重跑原副作用。
- 新记录仍是 surface/replace format 2，以 method=tool-fold 区分严格变体。
  保留 cut_seq、单项 source_seqs、source_digest、source_utf8_bytes、history_utf8_bytes、
  kept_recent_turns、policy_digest、replacement；没有 summarizer/summary/summary_truncated。
  共享源校验重算完整消息、来源、字节与闭合边界。原 manual/automatic 摘要变体继续合法。
  这是同一协议的明确判别变体，不是旧格式适配；旧实现不能读取 tool-fold，会明确拒绝。
  Session 10 / Context 9 / SQLite 2 / retained format 1 不变，不迁移或重写旧记录。
  CompactionPolicy 的 digest 配置版本变为 3，明确区分新的自动分层算法，四项输入不变。
- Surface 在原逻辑位置投影较短的 Tool reply。原 Session result、Effect outcome、retained_output
  永不改写；历史请求按当时截止序号重建。后续 M3 可把 fold 节点作为来源，History 递归展开回
  原 result 叶子；fold 自身不成为 History 目录根。Context 的 History 候选和 TUI 可见摘要统计
  只接受摘要变体，防止把工具折叠误报成可翻页的历史摘要。

## 生命周期、失败和显示

每条折叠用现有 expected_seq CAS 提交；并发冲突重新选择，不重投旧 payload。
每次取消都等待已发出的 append 收敛并对账，不留下后台写入。若已经完成一条折叠，后续折叠或
摘要失败不能声称“历史零改动”：CompactionError 带 compaction-after-tool-fold 前缀和
committed=true，表示至少已有折叠落盘；后续错误码仍保留。当前 Turn 按已提交投影继续。
单次写入的 false/true/unknown 对账规则沿用原 owner，没有回滚或第二状态机。

Line/TUI 从同一事件显示“旧工具结果已折叠”，详情不再把它当空摘要或人工摘要。
CompactionReport 返回本次最后一条成功替换；仅折叠时 method=tool-fold、summary 为空。
完整多条维护记录仍从事件日志读取，报告不成为另一份状态。

## 边界与验证

trigger 是可见对话字节数，不含完整请求的所有组成，也不是模型窗口百分比。C 不保证降至模型
窗口以内、不在活动 Step 中折叠、不自动开启关闭的压缩、不减少原文磁盘占用。
自定义宿主未装配搜索/读取工具时，原文仍可由宿主查账；通知不授予新的 Tool/Memory 权限。

验证见 [C 专题](../validation-tool-result-folding.md)：真实 Shell/SQLite、exit 0/7、模型搜索读回、
折叠后重启、近期/inline/关闭保护、提交失败、双取消、CAS、摘要后继与部分失败、请求重放，
并对真实发现的 Context 目录分类遗漏做反向验证。只跑 owner 与相邻定向检查，不跑全量或 L2。
