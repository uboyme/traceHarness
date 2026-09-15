# WC-1B：结构化只读协作候选实施

日期：2026-09-12。范围：WC-1B；尚未执行 WC-1C 真实模型验收，不代表新的发布默认策略。

## 改动与原因

Adaptive 同一主 Agent 先进行最多两步只读侦察，再在独立证据输入中提交一次 local/delegate。
local 不创建；delegate 由原协作 owner 派发并等待绑定消息的报告，再让主方执行。
不要求主方与助手同时干活，主方可以依赖助手先调查的结论。single 保持原流程。

新增通用 request/view 冻结/重建接缝，Product 从原 Session 解释阶段；没有新增 Planner、
Surface、状态表或调度器。Session 14 明确拒绝旧会话，Context 12、SQLite 2 不变。
详见 [ADR-0070](../adr/0070-source-bound-step-views-and-structured-collaboration.md)。

实现中实际定位并修复：

- collect 的机器 data 不含报告正文，组合返回必须保留原 content 中的 statement 与 evidence。
- tool/result 经 step_id 归属 Turn，不能误用不存在的 turn_id 筛选；证据以原 Surface 序号绑定。
- 连续拒绝提示不能替代本 Turn 最早的用户目标；失败结果明确保留为失败。
- ToolRuntime 在记录新调用之前解析视图，副作用前整批拒绝越界工具和超量调用。

底层 InvestigationControl、Supervisor、Budget、Workspace 与 Process 仍拥有原资源。
300 秒报告等待服从原更短工具/任务时限；取消先 stop、等收敛，再返回。
助手预算申请作为未完成报告返回，不拨款、不续派；主方仍负责核验和交付。

## 验证

最终 **244 项不同定向测试通过，零失败、零跳过**：请求/协议/Surface/Runtime、协作与 Product
合并组 210 项（393.59 秒），Budget/Process 相邻组 34 项（1.74 秒）。
`compileall src tests`、4100 项 collect-only、23 个本阶段修改 Python 文件 Ruff、文档相对链接/
代码围栏/主章节编号对应、`git diff --check` 通过。collect-only 只是收集，不是全量执行。
模型响应使用确定性替身；Git 工作区、Docker 验证、Supervisor 和持久化链路真实运行。
覆盖 local、delegate、非法/重复决定、混合批次拒绝、两次侦察拒绝、来源篡改、正文到达、
助手失败/partial budget report、Tool 超时与取消收敛、single 隔离及请求独立重放。

在临时源码副本中做了三项反向验证，均因预期行为失败，原工作区未被破坏：

| 移除保护 | 观察到的失败 |
|---|---|
| 整批工具视图拒绝 | 非法批次不再全为 denied，发生了原本禁止的执行 |
| 报告正文交接 | 主方报告缺少 statement |
| 取消捕获/收敛 | 调用返回时助手消息尚未 settled |

相邻 token 测试最初沿用“大文件读取全文”的旧假设；干净 HEAD 的旧读取实现通过，当前工作区
进入 WC-1B 前已有的分页读取只返回一页。核对该读取文件未被本阶段修改后，将该测试窗口从
6000 调为 4000，使首请求能进入、加入实际页后确实超过窗口；仍严格检查只调用一次模型、
工具调用与结果保留、没有折叠和不变量通过。没有放宽断言或修改生产预算默认。

未运行全量测试、L2–L4、Wheel、联网安装、真实 Provider 或额外基线；没有提交、推送、发版。
旧 DA 自动拨款实验依赖原自由派发合同，入口明确拒绝，不加载连接；历史复现须用冻结源码。

## 文档与下一步

正式/通俗上下文同步第 1 节状态、第 7 节及相关当前协议说明、第 14.3 节模块职责/流程/图、
第 15 节验证入口。执行计划与 CHANGELOG 同步；历史实验统计保持原冻结语义。

WC-1C 才做一次真实主方与助手完整任务：整树最多 16 次调用、600 秒，先冻结题目与连接；
验证报告实际进入请求、主方使用证据、原 Product completed、预算/工作区清理。失败或封顶即停。
通过前不进入 WC-2 写助手，不宣称自然分工稳定、协作收益已证明或新策略已发布。
