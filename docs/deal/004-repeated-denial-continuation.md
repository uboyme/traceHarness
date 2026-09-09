# 004：连续相同拒绝的 Continuation 保护

## 根因与职责

来源工具选错和拒绝后原样重试是不同问题。本次只收敛后者：原默认 Continuation 对包含 Tool Calls 的响应继续执行，直到预算/步数耗尽；没有针对连续相同拒绝的停止条件。

状态从 Session 已闭合 Step 投影，策略属于 Continuation。工具仍逐次检查权限；不缓存授权，不改 Projection/Reader 的来源准入，不引入第二事实源。详见 [ADR-0062](../adr/0062-repeated-denial-continuation.md)。

默认 2 次提示、3 次以 `stalled_repeated_denial` 停止。CLI 可调整 `--denial-warn-after`、`--denial-stop-after`，或 `--disable-repeated-denial-check`；关闭与显式次数互斥。SDK 使用 `RuntimeConfig.repeated_denial_policy`。没有新增 TUI 表单。

## 固定请求真实对照

[固定请求记录](../validation-data/active-retrieval/denial-fixed-request-01/summary.json)：4 对请求仅改变一个 tool message 的拒绝呈现，共 8 次真实调用、36,889 reported total tokens。下一步选对来源为 1/4 → 3/4；两对改善、一对均正确、一对均错误。这是单步选择，不是完整回答正确率；模型生成的调用未执行。JSON 呈现仍保留在隔离实验，未合入生产。

## 真实完整旅程

[汇总与逐例判定](../validation-data/active-retrieval/repeated-denial-live-01/summary.json)，[人工审阅包](../validation-data/active-retrieval/repeated-denial-live-01/review-packets.json)。冻结源代码已保存为同目录 `source-as-executed.zip`，两组共享源码，只改变保护配置。

| 范围 | 关闭保护 | 默认保护 |
|---|---|---|
| 4 个自然问题 | 2/4 | 2/4 |
| 专门要求重复被拒请求的压力例 | 6 次相同拒绝，7 步 | 2 次相同拒绝，3 步 |

压力例明确是人为触发保护的测试，不计入自然检索分数。开启组第二次拒绝后的提醒进入实际请求（snapshot seq 60），模型随后自行解释阻碍并结束；没有触发第三次拒绝的硬停止。`completed` 只表示循环结束，不代表完成原任务。

10 条旅程共 65 次模型请求，326,673 reported total tokens，unknown/estimated usage 均为 0。连同固定请求实验，本轮 73 次调用、363,562 reported total tokens。所有拒绝结果均无 Effect；输出准备各执行一次。重新打开 SQLite 副本独立重放 65 个请求，无重放/不变量错误，开始与结束 Attempt 数一致。

自然问题有一得一失，不能从本轮归因出检索准确率提升。交替工具或参数不会被误算为相同连续拒绝，也意味着该保护不能解决全部无进展行为。

## 定向验证

210 个不同的 owner/相邻测试最终通过，覆盖 Runtime、Continuation、预算、CLI、权限恢复、取消/重复取消、恢复、SQLite 重开、事件重放与架构合同。分批执行，不是一次全量：原 owner 72 项、相邻 137 项，新预算用例 1 项；相邻初次出现的两项源码冻结摘要失败，在 ADR 明确授权接口变化后更新摘要，最终 103 项合同/保护复验通过。

硬停止的公开 Runtime 测试确认三次拒绝即停止；反向验证在进程内移除 Continuation 保护后，测试因实际变成 `max_steps_exceeded`、8 步而失败。权限转为允许时工具真实执行，参数变化及其他结果打断计数，不以空执行充当通过。

未执行全量、L2–L4、安装/Wheel、发布、提交或推送。AR-D 的冻结发布门槛和 NO-GO 不变。两份上下文同步第 1、10 节；旧呈现实验的历史结论保留。
