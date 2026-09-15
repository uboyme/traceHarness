# 记录 075：自主多子 Agent 的真实 TUI 验收

2026-09-14。用户要求直接验证 Claude 已实现的能力：题面不指定助手数量，由主 Agent 自己拆分，并在真实 TUI 中确认多个子 Agent 的身份、对话和最终门禁。没有重新做实现审查，也没有运行全量或 L2。

## 验收口径

沿用记录 073 的五模块遥测材料与 `deepseek-v4-flash`。requirement 只说明每个模块的职责、接口和文件互不重叠，**没有写助手数量**；宿主最多授权 4 个直接助手，实际数量由模型决定。真实执行经过 Textual 配置页、聊天、提议、确认、START、Product/Workflow/Supervisor/Workspace/Patch/Verifier 主线；运行后从不可变账本副本重新打开真实任务对话页，不追加模型调用。

整树冻结最多 60 次 Provider 调用、900 秒、单次连接 60 秒、零模型重试。目标 Git ref 在人工批准前必须不变。

## 结果

主 Agent 自主提交了 **4 个 patch_author assignment**，分别负责 `telemetry_parse.py`、`telemetry_rules.py`、`telemetry_format.py`、`telemetry_stats.py`，把组合模块 `telemetry_report.py` 留给自己。TUI 的任务对话页显示一个主方和四个同角色但身份不同的助手；四者各有独立 agent_id、session_id、工具调用与用量。四份 Patch 分别为 929、1556、1775、825 字节，全部被主方读取并形成 4 条 applied 整合回执。

真实运行启动 34 次模型调用，33 次有 exact usage：309806 input、21952 output、已知合计 331758 tokens；最后一次在失败流程退出 TUI 时被取消，没有 usage，故不伪造完整总数。墙钟 356.266 秒。34 份请求快照可重放；6 个预算账户关闭、35 个 usage reservation 结算；1 个工作区释放，5 个失败工作区隔离留证；1 次沙箱执行 finished 且 converged。原数据库哈希保持 `30857329d646ff4591a3a0cfa470519082b41ecab43788a99ae4f196974551ba`。

完整端到端结果是 **failed**：固定 Verifier exit 1，Product/Workflow 以 `workflow-agent-message-failed` 收口，所以没有 Review、Approval 或 Promotion，目标 ref 仍是源 revision `371b6caca426921da859f487f4dcbbf0d1507857`。TUI 结果卡显示 `任务失败 · 叶子失败：product-role-coder · unavailable`；这行没有呈现已经持久化的固定检查失败细节，是本轮暴露的展示限制，不能把 `unavailable` 解读成 Provider 不可用。最后一个 Provider attempt 的取消发生在任务已经失败后，由 TUI 退出收尾触发。

因此本轮结论分三层：**自主拆分通过**（无数量提示仍选 4）；**多子 TUI 展示与 Patch 整合通过**（四个身份、四份补丁、四条回执）；**完整功能验收未通过**（固定检查 exit 1，人工门禁未开放）。一题一次不能证明稳定性、提速或质量收益；记录 074 对这类小模块任务的同题对照仍是 multi 成本更高。

## 验收驱动修正

本轮先误用了记录 072 的“明确要求两个可写助手”夹具。用户指出口径错误后立即取消：18 次调用、148.375 秒、无 Promotion；它排除在本次验收结论之外，不用于证明自主拆分。

正确试次之后又发现驱动的第二次 TUI 配置保存没有携带原文件字节，触发 ConfigForm 的并发修改保护，但驱动未检查表单是否成功关闭。因此这次实际运行用了通用驱动覆盖后的较小预算；它仍成功开出四个助手并走到固定验证，失败不是额度耗尽。驱动现已传入 `expected_bytes`，通过 TUI 表单重写任务、主方和助手的全部冻结上限，并在保存失败时立即报错。零模型预检确认新目录保存为任务 2000000、主方 1600000、每助手 50000 tokens，主方 `max_children=4`、单 Turn 600000 ms。本轮不为追求绿灯追加真实运行。

收尾定向验证：`tests/test_tui_config_forms.py` 11 项通过；`tests/test_tui_task_conversation.py` 与 `tests/test_product_observation.py` 合计 32 项通过；账本副本的真实 PTY 对话页再次打开成功并识别四个 patch_author，零模型调用。`compileall`、4 个相关 Python 文件 Ruff、4255 项 collect-only、文档链接/章节/JSON/秘密/尾随空白及范围 diff 检查通过。未跑全量、L2–L4、Wheel、安装或追加真实模型。

精简机器证据见[自主多子 TUI 摘要](../validation-data/dynamic-collaboration/autonomous-multi-child-tui/summary.json)。
