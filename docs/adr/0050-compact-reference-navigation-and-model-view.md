# ADR-0050：精简模型参考视图与显式阅读导航

- 状态：Accepted（实现及验证状态以正式上下文为准）
- 日期：2026-09-08
- 范围：v0.9 Stop C 修补 C2，原 Context renderer、共享 prompt 与引用 Tool
- 依据：[C2 验证记录](../validation-v0.9-stop-c-c2.md)
- 调整 ADR-0049 的模型展示合同，保留其位置、来源资格、收据与正文保留规则。

## 问题与证据

Memory、History 与混合资料的真实旅程出现未展开目录、漏读下一页和错误填值。仅补阅读动作或
重述当前任务没有稳定解决问题。原模型视图重复展示来源证明、事件引用和观察摘要，少量正文被
大量协议字段包围；宿主阅读导航与来源正文又被同一段“不可信数据”说明笼统包裹。

评测初版也存在取值形式歧义：只要求字符串，却没有说明数量不带单位、文件状态只取代号。先在
新 format 2 语料中公开逐字段要求，保留旧结果不改分，再冻结相同题目、答案和门槛做新的完整对照。
现有实现的三模型核心成绩为 20/28、27/28、28/28；单独增加阅读动作的候选为 22/28、28/28、28/28。
精简展示并区分导航与正文后为 27/28、28/28、28/28，原 SQLite 核算确认来源、重放和不变量违规为 0。
原 Skill 回归为 24/24、23/24、24/24，达到事前门槛。有限样本不承诺所有模型完全遵从指令。

## 决定

1. `context/input` 继续保存完整 block、source_refs、provenance、来源边界和收据证明。模型视图仅由
   原 `_render_item` 从这些合格块派生；不增加事件流、可变消息事实源、读取缓存或另一条请求主线。
   Inspector 与 reader 仍可检查完整证明，旧请求仍按原冻结前缀重建。
2. 模型项共有 `kind/id/version/tier/digest/body`。Skill 额外显示 `catalog_digest` 与固定说明；
   Memory 显示项目、fact_slot、body_status 和 read_action；History 显示 body_status、
   current_workspace_validity、more_pages_available 和 read_action。重复的账本证明不再放入模型项。
   正文保持原字节，经同一 canonical JSON 编码，不能成为系统指令。
3. Memory directory 的 read_action 复制当前 memory_id/version，申请完整短事实 section；已有
   summary/section 正文时为 null。History 按原有界 reader 的首 cursor 或 next_cursor 生成 chunk
   阅读请求；没有 reader 或已到末页时为 null。动作只描述原 Tool 的参数，不注册新 Tool、不授予
   权限，也不绕过资格、政策和预算检查。unknown/stale 表示不能据此证明当前工作区，不表示历史
   页面不可读。动作不会自动执行，模型仍选择需要的资料并发起原工具调用。
4. 固定头尾区分宿主导航与来源正文，明确当前任务及 Tool policy 的优先级；共享 prompt 和 Skill
   Tool 描述使用同一个模型视图合同。完成提示要求遵守用户指定的答案格式，并继续读取缺少的证据。
   不根据评分器反馈强制工具调用，不裁剪模型答案、不补值、不转换非法参数，不按模型或题目设例外。
5. 原唯一预算计入实际显示的导航、正文、转义和头尾。预算减少来自模型视图变短，原配置限额不变；
   放不下仍整块排除。C1 的新请求优先、连续准入、每步资格复核、撤销、取消、失败及跨轮退出保持原规则。
6. 唯一切换为 Session 9、Context 8、context-json-v8、f5-context-policy-v5。Session 8 / Context 7
   只用于保留的隔离导航探索。旧 Session 1–8 及旧 Context/renderer/policy 明确拒绝，不兼容、迁移、
   改写或删除。披露 receipt 2、History page policy v2、SQLite 2、M3 2、来源检索 receipt 2 和词法
   tokenizer v3 / ranker v2 不变。

## 验证与边界

检查原 Runtime 的阅读动作、未授权 Tool 拒绝、连续分页、预算边界、正文保留与撤销、请求重建、
旧协议拒绝及相邻检索回归。真实 C2 与 Skill 网格独立从 SQLite 核算，保留所有失败与用量。
格式化前后用源码摘要与 AST 等价记录绑定，并在接入后的源码上执行定向检查。

这项决定不增加向量检索、跨 Session 历史搜索、模型查询扩写或自动完整资料加载。
一次纯 JSON 格式偏离、一次 Skill 非法披露请求及后续被拒的工作区调用仍作为真实失败保留。
C3、C4、Release Stop C 独立审查及发布门禁另计，不因本 ADR 宣称完成。
