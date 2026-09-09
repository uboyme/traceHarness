# ADR-0062：由原事件派生连续拒绝停滞信号

状态：已采纳（实现与本阶段定向验证同步）。

## 背景

真实检索轨迹存在不同 Step 中相同参数的工具请求反复被拒绝，最终仅由 max_steps 结束。
拒绝不是执行失败后的自动重试，而是模型重新选择同一调用。单纯增强拒绝文字不提供确定性的收敛上界。
本次用户明确授权固定请求对照与范围有限的无进展保护，不实现通用语义进展判断。

## 决策

- `RepeatedDenialPolicy` 显式配置 warn_after/stop_after，默认 2/3，允许配置 None 关闭。
  每个 Turn 在原 turn/start 中记录该策略；投影以该 Turn 原事件为依据，没有可变跨回合计数或第二事实源。
- 只统计连续的完整 model_response Step：整批调用都得到未执行的 denied 结果，工具、规范参数、
  拒绝状态/错误/原因/data 与 Composition revision 一致。批内调用顺序不影响身份，每批只计一次。
- 先走原 ToolRuntime 当前权限判断，之后才检测。成功、其他错误、参数/拒绝结果/Composition 变化、
  不完整步骤及新 Turn 都打断连续计数；不缓存拒绝来跳过权限检查。
- 这里的稳定性指已记录的连续决策结果，不能识别尚未记入事件的外部权限变化。
  并不保证下一时刻授权不会变化；停滞结束不撤销授权，用户仍可在新 Turn 重试。
- DefaultContinuationRuntime 在第二次拒绝后沿已有 Continue.messages 给出一次调整行动的提示，
  第三次同样拒绝后 Finish(stalled_repeated_denial)。max_steps 的硬上限优先，不宣称任务成功。
  BudgetContinuationRuntime 传递同一派生信号，仍执行原预算对账；自定义 Continuation 拥有其决策。
- AgentLoop 仅记录配置、读取原 Session 事实、传递投影结果和执行继续/结束指令；不识别领域问题，
  不决定工具授权，不新增后台任务、工具拦截器或重试状态机。

## 协议与恢复

已有模型消息投影规则不变；警告使用已有持久 user/message 反馈入口，当前任务仍由本 Turn 首条用户消息绑定。
新 turn/start 策略是可选配置事实，缺失或 null 表示没有该策略，适用于显式关闭及原历史记录，不猜测历史阈值。
Session 11 / Context 10 不切换；不重新解释或改写旧请求。若以后采纳结构化拒绝呈现，需另行冻结投影合同。
取消仍由原 owner 收敛并关闭 Turn；恢复不在未完成步骤上累加。新 Turn 从自己的起点重算，跨进程重读结果一致。

## 边界与后果

这是连续相同拒绝检测，不是通用 loop detect：不拦截成功轮询、参数不同的翻页、交替工具循环或语义上重复的不同参数。
不能用减少调用量代替正确转向或成功完成。默认阈值是显式产品策略，不从测试题、模型或领域关键词推导。
CLI 提供阈值与关闭参数，恢复命令保留设置；SDK 通过 RuntimeConfig 配置。

受保护 AgentLoop/AgentRuntime 的改动限于该通用继续执行接缝及配置传递；Supervisor、PluginManager 字节不变。
相应保护摘要在定向行为检查、反向验证与原架构检查后重新冻结，不删除保护测试，不放宽 Product/Workflow 隔离规则。
