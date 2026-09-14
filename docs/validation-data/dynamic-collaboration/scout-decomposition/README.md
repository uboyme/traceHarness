# DA-9 有界侦察后决定：验证数据

本目录只保存无秘密的精简实验记录，原请求正文、SQLite、CAS、冻结源码及候选测试归档留在本机忽略目录。

| 文件 | 含义 |
|---|---|
| contract.json | Provider 连接前冻结的材料、源码、驱动、模型策略和调用上限 |
| preflight.json | 断网 Docker 的空产物反例与参考产物正例 |
| summary.json | 原 driver 统计，保留原貌 |
| independent-replay.json | 9 个会话、24 份请求在临时数据库副本中的重放和不变量检查 |
| analysis.json | 按原主 Session 事实校正的停止原因、角色标签和撤回结论 |

原 summary 的 termination=completed 表示 driver 没遇到网络失败或调用上限，不能当作任务成功；两个失败 Product 的真实错误是 product-decomposition-decision-missing。原 provider_calls_by_role 的 unknown 是计数器未识别收窄后的主方工具面，独立复核确认 18 次均来自主方，无助手调用。

三题均实际完成有界侦察，读取内容也进入决定请求；可拆、耦合题都没有提交决定，简单题在第三次决定请求才选择 local 并完成。总计 18 次真实调用、65117 exact tokens；Product 与完整行为均为 1/3，Provider failure=0，预算 3/3 收敛，Workspace live=0。没有确认组、基线、语义裁判或采用。详情见[记录 035](../../../deal/035-scout-before-decomposition.md)。
