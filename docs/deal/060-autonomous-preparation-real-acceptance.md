# WC-4 自主准备与一次真实验收

2026-09-13，用户明确要求主方自主侦察理解需求，再按职责分工，并授权简化后跑一次真实验收。旧四轮失败不覆盖，保留现有未提交改动，不提交推送发版。

## 根因与修改

记录 059 在连续读取需求时碰到固定两步和每步一个工具限制；这些是产品策略，不是职责或权限所必需。按 [ADR-0074](../adr/0074-autonomous-collaboration-preparation.md)，移除 scout 阶段，将原只读工具与分工工具同时提供，保留原可见会话，主方决定调查深度与分工时机。仍沿原总预算执行，无独立无限循环。

原 ToolRuntime 的 schema 拒绝、明确未执行的工具拒绝与 delegate 前字段校验错误可修正。原 Session 结果区分可以修正和已经派发/未知/失败的情况；同批多个计划全部在派发前拒绝，已接受的分工不能重复派出。没有修改 AgentLoop、Provider、工作信封、权限、Supervisor/Workspace/Artifact/Evaluation 所有权、Patch 整合或人工 Promotion。

通俗例子：主方可以一次读需求、接口和约束，继续查清疑点后，把具体职责交给助手；表格填错可以修改，已派出的任务不能重派。主方仍不能在准备阶段写文件，助手不能越权，最终必须实际整合和检查。

## 定向证据

自主准备、原循环与请求重放 18 项通过：四轮各三个文件实际读取；无错误、schema 错误、空白字段、同批重复计划和混入越权写入均覆盖，纠正后只有一个助手。取消、等待超时、总步骤耗尽、无分工交卷和复核取消/预算耗尽沿原 owner 收口。

进程内临时恢复 max_calls=1 后，九个实际 read_file 请求被拒，原重复拒绝机制停止，新用例按预期失败；独立正常进程恢复 18 项通过，没有回滚用户文件。另有 Product、Patch、固定完成检查、评估及优化相邻 62 项通过，共 80 项不同用例，零跳过；一处旧调用次数断言已适配自主准备，定向复测通过。compileall、4201 项 collect-only、9 个本轮 Python 文件 Ruff 和 git diff --check 通过。空实现预检 exit 1、参考实现 exit 0。

## 真实验收冻结

复用未改动的 writable_acceptance 驱动和材料生成器；新目录 `.traceh/wc4-autonomous-20260913/`。主方与助手均用既有项目选择 deepseek-v4-flash，保留连接，不加模型特例。仅一题一次、主子共 32 次调用、600 秒、单连接 60 秒、零重试；先确认空实现失败、参考实现通过。此唯一调用已结束，结果见下；完整 WC-4 未通过。

未运行全量、L2–L4、Wheel、安装、镜像拉取或收益对照。不提交、推送或发版。

## 真实结果与剩余问题

本轮一次真实 DeepSeek V4 Flash 共 32 次调用（主方 26、助手 6）、245527 Provider exact tokens、224842 ms，无 Provider 失败或重试。前两轮分别成功批量 read_file/list_files 和两个 read_file；第三轮分工完成，助手交出 980 字节 Patch。原交接正文实际进入后续 23 份主方请求；旧 reports_dispatched_to_parent 指标仍为 0，不能据此认定没有送达。

主方 14 次 read_child_patch 均显式请求 count=20，仅连续读到 280/980 字符；另外 9 次请求当前未提供的 read_tool_output/list_tool_outputs/search_tool_output 被拒。公开 schema 没解释 count/offset 单位、默认值或范围，说明又引导 read_tool_output，而本轮实际工具表没有它；这些是可复核的接口表达问题，不能证明模型内部为何选 20。现有实现默认 2000、上限 4000 字符，本 Patch 本可一次读完。主方没有整合、改代码或进入完成检查/正式 Review；最终原 Budget 在下一次 admission 以 max_tokens 耗尽停止，并非 Provider 调用上限包装器触发。源码静态核对还发现助手用 if not rows 返回 []，遗漏最外层 list 校验；本轮未执行功能验收，不能把它报告的“全部满足”算通过。

3 个账户关闭、34 个使用预留结清，2 个工作区释放、1 个隔离留证、live 0。预算结算 240000，与 Provider 报告不同；两笔使用量质量 unknown，不用结算数冒充实际 tokens。34 份请求在数据库副本上重放及不变量检查通过，原库与前四轮证据不变。80 项不同定向/相邻检查、反向验证、compileall、4201 项 collect-only、9 个 Python 文件 Ruff、文档和 diff 检查通过。自主准备实现完成，WC-4 完整验收仍失败；没有追加真实调用、全量/L2、收益对照或项目提交。

原数据库 SHA-256：`25a9f85f752752b0b75e0892f2eb613900fa9b776731c56d462bd3c0fe81f922`。分享证据见 [summary.json](../validation-data/dynamic-collaboration/writable-autonomous/summary.json)。execution.stopped=false 表示 Provider 包装器未熔断，Runner 已结束且 Product/Workflow failed；32 次实际调用没有超上限，最后触发的是原 Token 预算门禁。

本轮修改正式/通俗上下文 13.11、14.3、14.3.9、15、16 的状态、流程和限制，通俗装配说明同步自主准备；入口、导航、执行计划和交接状态同步。没有在真实失败后修改读取工具追跑或追加调用。后续应先修正读取工具的参数单位/默认值说明，以及仅指向已提供能力的导航；不应通过放开权限或加预算掩盖问题。
