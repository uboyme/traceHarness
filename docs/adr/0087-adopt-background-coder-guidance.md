# ADR-0087：人工采用一条通过验证的后台建议（coder 执行说明）

状态：**已决定**（用户批准）。日期：2026-09-24。修改 `product/execution.py` 的白名单文本 `CODER_GUIDANCE`；
不改任何合同、协议或权限。

## 背景

收口实验 C2（[记录 087](../deal/087-task-type-context-evolution.md) §4.5）：

- 后台宿主在 6 次 SWE 开发运行上检测到 `tool-failed`（4 个来源）。检测附带宿主记录的原因
  （[ADR-0085](0085-evaluation-detections-quote-host-causes.md)），原因是模型把 `cd`、`PYTHONPATH=src`
  这类 shell 语法写给按 argv 执行的 shell 工具，导致沙箱 `start-failed`。
- 分析模型据此提出候选 `a9837ab4`（候选文件 SHA-256 记录在
  [c2-selection](../validation-data/task-type-evolution-v1/c2-selection.json)）：
  在 coder 说明里要求“用直接的可执行命令运行检查，避免 `cd` 等 shell 内建命令和行内环境变量赋值”。
- 用户选择验证。3 道验证题 × 2 次基线/候选配对，经预登记修正后 6 组全部有效：
  通过 2/6 → 4/6，没有“候选失败而基线通过”，满足预登记采用门。

系统合同规定后台与评测永远不会自动采用（报告中 `adoption_authorized=false`），采用必须由人完成。

## 决定

采用该候选。落地方式：

- 用项目自己的 `apply_candidate`：核对原文 `old_sha256`，只替换该字符串常量的字节范围，并做 AST 等价校验。
- 候选绑定的源码摘要是 `bbe085dd`（验证时的源码）。当前为 `9dcaf759`，差异只在 ADR-0086 的三个评测文件
  （`evaluation/plan.py`、`evaluation/runner.py`、`tui/optimization_plan.py`），
  `product/execution.py` 逐字节相同，所以把同一份编辑改绑到当前摘要后应用，结果等价。
- 应用后仅把单行字符串拆成多行、补一行来源注释；字符串值与候选 `new_text` 逐字节相同，
  而白名单校验按字符串值计算。

## 后果

- 此后 Single 的 coder 说明以新文本为基线；已完成的实验结果仍按各自冻结源码解释。
- 旧的 `CODER_GUIDANCE` 候选及后台周期里记录的原文摘要会按原规则因文本漂移被拒绝。
- 证据有限：增益集中在 pyvista 一题（候选 2/2，基线跨轮合计 1/4），每臂两次。
  对应的机制（shell `start-failed`）在验证中基线 4/63、候选 2/50，方向一致但计数太小。
  这是“通过预设采用门后人工采用”，不是“已证明普遍提升”。

## 被否决的方案

- **不采用、只保留实验结论**：链路停在验证，没有完成设计中的“人工采用”一环。
- **手工改写文本**：不经过 `old_sha256` 与 AST 校验，无法证明写进源码的正是被验证的那段文字。
