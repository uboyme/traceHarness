# ADR-0049：当前参考后置与本轮有界正文保留

- 状态：Accepted（设计冻结；实现/验证状态以正式上下文为准）
- 日期：2026-09-08
- 范围：v0.9 Stop C 修补 C1，原 Context / Request / 引用 Tool 主线
- 依据：[执行计划及真实诊断](../plan/TRACEHARNESS_V0.9_RELEASE_STOP_C_EXECUTION.md)
- 调整 ADR-0043、0045、0048 的当前消息位置与正文寿命决定，不改写历史记录。

## 问题与证据

当前 Context 放在全部旧 Surface 之前，后面工具回执却说“下一步再读”。真实模型会忽略已经送达的
目录，等待或重复请求。相同八次探索旅程中，原方案严格通过 4 次，位置后置并同步定位说明通过 8 次；
仅改回执时态仍为 4 次。样本不足以证明统计显著性，最终重复验收独立执行。

显式正文在一个 Step 后退出，使连续读取两章或两页时第一份原始证据丢失。要求模型先记笔记只能缓解，
不能保证资料仍可核对。公开 Runtime 反例证明：顺序请求两个章节，第三次请求只含第二章；临时恢复
旧寿命后，新测试按该原因失败。问题属于请求呈现与披露生命周期，不属于增加向量检索就能修复的范围。

## 决定

1. 唯一 Context message 位于完整 Surface 之后，包括首步、Tool 和 Verifier 续步。它仍是宿主生成的
   request-only user-role 参考包，不是新的用户需求；只有原 canonical JSON 字符串携带正文。空 Context
   使用相同位置和 renderer。构建、校验、重放、披露资格证明和 Inspector 使用一致的位置合同。
2. 引用请求首次仍只面向同 Turn 的紧邻下一 Step；typed History 用户输入面向该 Turn 首 Step。
   只有实际进入该 Step Context 的正文才可继续保留。新请求、已保留请求和预算结果只从原 Session
   事件推导，没有新的 Stream、pending 缓存或可变 messages 权威。
3. Skill section/chunk、Memory summary/section（两者均为完整已批准短事实）、History section/chunk
   可以沿同一 Turn 的连续已注入 Context 保留。目录和 Skill 摘要不获得保留资格。每步重新取得原
   qualified source：Skill 仍核 selection/catalog/version/同 Lease 资源；Memory 仍核 project/active/
   activation；History 仍按原 block/cursor/来源图读取并重算当前 freshness。旧正文不因此成为当前事实。
4. 新显式请求先于保留项；同组按 History、Skill、Memory 及原请求顺序，保留项沿最近一次注入的次序。
   然后考虑自动 History 目录/摘要和原 Skill/Memory 融合候选。所有来源共用一次 item/kind/total/block
   预算分配。完整块放不下就淘汰，不裁剪；自动词法覆盖支配只用于自动 Skill/Memory 候选。
5. 首次没注入、途中淘汰、授权失效、来源不一致、失败、取消、恢复或 Turn 结束都会中断保留资格。
   未再次明确请求时不能复活。保留不扩展 max_requests 的单步新请求额度，其数量受前一步实际 Context
   的 max_blocks 约束。同 Step Provider retry 仍只复用已冻结 bytes，不重新读取或延长授权。
6. History 的 raw provenance 继续指向最初授权的原 history/requested 或 tool/result，保留链由同 Turn
   每一步实际 Context 证明。Skill/Memory 同样从原收据与连续注入推导；当前源与历史 frozen request
   继续分别验证。过去的请求不读今天的资源、索引或 active 状态。
7. 通用 Tool 描述明确各 tier 的适用字段，unused IDs 必须为 null。错误不得通过猜测 ID、接受冲突
   字段或硬编码样本来“修复”。正文不进入普通 Tool result/Surface，模型笔记不升级为事实源。
8. 唯一切换为 Session 7、Context 6、renderer context-json-v6、policy f5-context-policy-v3。
   引用 Tool receipt format 2，target_rule=next-step-then-bounded-turn；History policy identity
   为 history-turn-pages-v2。SQLite 2、M3 2、来源检索 receipt 2、词法 tokenizer/ranker 不变。
   旧 Session/Context/receipt 明确拒绝，不双读、迁移、改写或删除旧数据。

## 验证与代价

要求公开 Runtime 的正向、预算淘汰与不复活、跨轮/取消/失败、撤销/替代、catalog/resource 漂移、
History 连续页与新请求优先、相邻 Request/Inspector/Tool 回归，以及恢复故障逻辑的反向验证。
最终真实模型重复验收按执行计划预先冻结的标准运行，失败计入分母，不以探索结果或脚本 Provider 替代。

正文在单轮内可能多次进入冻结 Context，因此增大该轮请求字节与事件体积；原预算和闭合 Turn 边界
仍给出上限，不增加新的 TTL 数字默认值。语义检索、Memory/History 的真实旅程与 Stop C 独立审查
在后续步骤完成；本 ADR 不宣称这些门禁已通过。
