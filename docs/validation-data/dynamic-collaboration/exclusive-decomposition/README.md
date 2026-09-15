# DA-8 独占拆分界面验证数据

本目录保存 DA-8 两轮显式真实实验的精简、无秘密证据。原 Session、Request、Effect、Workspace 和运行目录留在本机忽略目录，不提交；这里的 JSON 不含 API Key、响应正文或本机路径。

| 文件 | 含义 |
|---|---|
| `run-1-contract.json` / `run-2-contract.json` | 各轮在连接 Provider 前冻结的题目顺序、源码/驱动/沙箱摘要、调用上限与禁止采用声明 |
| `run-1-preflight.json` / `run-2-preflight.json` | 固定断网 Docker 中空产物失败、参考产物通过的反向预检 |
| `run-1-summary.json` / `run-2-summary.json` | 原驱动在关闭运行后写出的统计；首轮 `first_step_tools` 含已知跨 Session 分析错误，保留原貌 |
| `analysis.json` | 对原 SQLite 做只读复核后的合并结论和撤回决定 |

首轮三项任务均通过硬验证，三项决定也符合期望；可拆题创建并运行了助手，但主方没有 collect，因此按联合合同只有 2/3 完整行为通过。首轮分析器跨 Session 取错 Composition；只读复核确认主 Session 的首步实际均只公开决定 Tool。

修正分析器并补上子工作显式收口后，第二轮三项首步工具面均正确。可拆题却选择 local 且任务失败，紧耦合题选择 local 但任务失败，只有简单题完整通过。两轮合计 54 次真实 qwen-plus 调用，六次 Budget 均收敛，Workspace live=0，Provider failure=0。候选未采用。
