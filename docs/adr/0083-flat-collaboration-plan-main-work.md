# ADR-0083：分工计划的主方工作改为三个顶层参数

状态：**已决定**。日期：2026-09-23。修订 14.3.1 所述 `submit_collaboration_plan` 的输入形状；
工作包、分工校验规则、字段长度与所有权限/预算/收集语义不变。

## 背景

收口计划实验 A 的前三份配对中，Multi 臂 3 次有 2 次在提交分工计划时以
`provider-tool-arguments-json-value-expected` 失败，整个 Turn 中止；Single 臂从未出现。记录 086 §4.20
中断 8 题扩样的也是同一失败码。adapter 按设计不记录响应正文，事件日志无法定位原因。

用 `tests/task_type_evaluation/replay_probe.py` 把两次失败的冻结请求经 adapter 自身的 `_complete_sync`
原样重放并保存原始响应（直连，不执行工具，不写 Session），证据见
[诊断记录](../validation-data/task-type-evolution-v1/tool-arguments-diagnosis.json)：

| 同一冻结请求的变体 | 参数非法 |
|---|---:|
| 原样：嵌套 `main_work` 对象（存储规范化后按字母排在 `children` 之后） | 7/8 |
| 仅把 `main_work` 移到属性首位 | 3/9 个调用（另有 3 个调用 JSON 合法但缺少 `main_work`，仍无法通过校验） |
| 把 `main_work` 拆为三个顶层字符串 | 0/8，且三个值均为非空字符串 |

失败时 `children`（对象数组）完整，`main_work` 的值缺失（`"main_work": }`）、整个键缺失，或漏出
`<parameter name` 等模型内部工具模板文本。初始假设“属性顺序”被对照否定；该模型/服务组合在
**顶层对象型参数**上不能稳定产生合法参数，而对象数组与顶层字符串稳定。

## 决定

`submit_collaboration_plan` 的输入 schema 以 `main_goal`、`main_deliverable`、`main_uses_child_report`
三个必填顶层字符串代替嵌套 `main_work` 对象，长度限制沿用原 `MAIN_LIMITS`。宿主在校验与派发时用
`plan_main_work()` 组装回原 `main_work` 对象，助手工作包（readonly work 2、writable work）与报告
归属保持原结构。提示中的字段名同步更新。

## 后果

- 旧形状（含 `main_work` 的计划参数）作为字段集不合法被拒绝，返回原有的可纠正拒绝；不做兼容别名。
- 历史运行的 `tool/call` 事件保留旧参数原样；只读投影只读取 `children`，不受影响。
- 未放宽任何解析规则：非法 JSON 仍是 Provider protocol 失败；本决定只移除一个已被证实不可靠的参数形状。
- `ALLOCATION_GUIDANCE` 文本变化会改变其 AO 可编辑文本摘要；旧候选按原规则因文本漂移被拒绝。

## 被否决的方案

- **按 `required` 顺序重排 schema 属性**：对照证明顺序不是主因。
- **把非法参数改为可纠正的工具拒绝**：改变 protocol 失败语义并要求记录不可解析的参数，范围更大；
  在已找到确定性根因时不需要。
- **更换模型条件**：会使已完成的 Single/Multi 结果与后续不可比，且问题出在我们可控的参数形状上。
