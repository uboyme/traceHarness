# ADR-0079：派发调用的等待时长由宿主授权推导

状态：接受。日期：2026-09-14。修正 ADR-0077 在 `await_report` 上的一个实现常量，不改写其决定。

## 原因

ADR-0077 记录 `await_report` 在派发调用内部等待精确报告，上限写成常量 `CHILD_REPORT_WAIT_SECONDS = 300`。但 Product 主线的每一次工具调用都运行在 `ToolRuntime` 的 `asyncio.timeout(tool_timeout_seconds)` 里，而 Product 从未设置该值，沿用 `RuntimeConfig` 默认的 **60 秒**。因此那个 300 秒在生产路径上**从未生效**：

- 宿主在 Profile 里授权助手 `max_wall_milliseconds`（实验配置常用 240000），计划工具却承诺一个自己无权持有的等待；
- 批次一旦超过 60 秒，外层 `asyncio.timeout` 取消该工具调用，`_converge` 停掉**仍在自身预算内**的助手，工作全部作废；
- 计划结果是 `status=failed`/`error_type=TimeoutError`，不属于可纠正输入，Continuation 以 `CollaborationDispatchFailed` 终止整个任务。

反向验证复现了这条路径：恢复固定等待后该用例耗时 **60.10 秒**，计划结果 `('failed', 'TimeoutError')`，目录里留下**一个被创建又被停止的助手**，运行以 `CollaborationDispatchFailed` 结束。两个 owner（宿主授权与运行时上限）口径不一致，是实现缺陷，不是参数没调好。

## 决定

不变量：**这次调用永远不得等待少于宿主已经授权的助手运行时间，也不得承诺其运行时会截断的等待。**

1. 等待时长从宿主授权推导，不再是固定数。批次并发执行，因此下限取各助手 grant 中**最长**的 `max_wall_milliseconds`，不是求和。该数据由既有的 `_require_capacity` 预检读取——它本来就为校验批量维度遍历过同一批 grant。
2. 计划工具显式接收"一次调用可持有多久"（`in_call_wait_seconds`）。Product 由同一个 `TOOL_TIMEOUT_SECONDS` 同时提供给 `RuntimeConfig.tool_timeout_seconds` 与计划工具，两者不能各自漂移。`COLLECT_RESERVE_SECONDS = 5` 留给等待结束后仍在同一次调用内进行的报告收集。
3. 授权装不进该上限时，`await_report` 计划在**任何助手被创建之前**以可纠正的 `CollaborationPlanInputInvalid` 拒绝，消息点名 `dispatch_and_continue` 与对应收集工具。宿主未声明 grant 时，`CHILD_REPORT_WAIT_SECONDS` 退化为兜底上限，并仍受该调用自身上限约束。

## 被否决的替代方案

- **把 300 改大**：无效。真正触发的是外层 60 秒。
- **全局调大 `tool_timeout_seconds`**：为一个委派场景放宽所有工具的时限。宿主若确实需要长时 `await_report`，现在可在同一处改。
- **超时后降级为 `outcome=dispatched` 而不是收敛**：能保住已完成的工作，但会出现第二条到达 dispatched 状态的路径，而视图暴露与交付门禁读的是计划请求的 handoff 模式——同一事实两个来源。按既有事实源规则否决。

## 边界

协议版本、计划输入字段、工作信封与收集工具都不变。在 Product 当前 60 秒上限下，助手墙钟授权较大的任务实际走 `dispatch_and_continue`——这也是主方不空等的唯一模式。本 ADR 只改等待时长的来源与不可持有时的答复，不改权限、修订版本、捕获、验证、人工审批与 Promotion 的任何边界。
