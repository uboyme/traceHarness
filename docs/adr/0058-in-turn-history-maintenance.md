# ADR-0058：轮内维护仍只处理闭合旧历史

状态：E1 已实现并通过定向与真实验证，2026-09-09。扩展 ADR-0057 的首步限制；不改变其来源、费用和审批合同。

## 决策

工具结果会增加同一 Turn 后续请求的压力，因此 RequestBuilder 在每个尚未冻结的 Step 都可以调用原 CompactionService。
来源仍仅为已闭合旧 Turn，保留配置的最近轮数，绝不切当前 Turn 的工具调用、参数和结果。
没有新压缩器、Provider 调度器、可变 messages 或第二事实源。完整请求继续由 E0 计量并执行硬上限。

规则摘录不占模型 Step，最后一步仍可执行；语义摘要占一个普通 Step，必须给后续回答留一步。
同一 Turn 最多出现一条 summary/input 调用链；同一步传输重试仍使用原许可和重试策略。
summary/input reader 按事件验证当前未冻结 Step、完整旧来源和不存在前序摘要；不再假设该 Step 必须是首步。
事件格式不变，增加的是可接受的合法位置；不改写已存在记录，不引入旧格式猜测。

## 披露生命周期

插入摘要前，使用 Skill、Memory、History 既有 eligible reader 检查当前 Step 的 fresh/retained 请求。
存在这些请求时不插入摘要，以免消耗下一步阅读机会；显式 TurnInput.history_requests 保持原整轮跳过规则。
没有可处理旧历史、持有阅读请求或没有剩余回答步骤时，仍可因硬上限拒绝，不能隐式丢弃当前资料。

共享 collect_requests 允许经过真实 summary/input 对应的 semantic_summary Step 结束继续检查后续正常步骤。
摘要步骤没有 Context，不携带旧正文授权；失败、取消和实际逐出规则不变。
这也修复了 D 的问题：摘要之后才发生的正常阅读工具回执，曾因更早摘要步骤的结束原因被整体忽略。

## 失败与边界

无效摘要记原失败事件，旧历史保留；在硬上限内继续回答。取消、Budget、重试、CAS 和恢复沿用 D 的 owner。
摘要期间不再次执行已经完成的工具。当前 Turn 本身过大、摘要输入过大或没有可压缩旧来源时，本阶段不保证继续成功。
E2 的参考 token 准入与 E3 的可读超限提示另行实现，不在 E1 偷换为工具结果字符串截断。

验证入口：[定向测试](../../tests/test_in_turn_compaction.py)、[真实及反向验证](../validation-in-turn-compaction.md)。
