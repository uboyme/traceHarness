# DA-6：Adaptive 任务拆分说明合同

范围：让 Adaptive 主 Agent 在大范围读取前识别一个可独立交给只读助手的问题。同一份 `DELEGATE_GUIDANCE` 同时装配为 Adaptive 专属 system prompt section 和 `delegate_investigation` 工具说明；不修改 Single 基础 prompt、助手 prompt、AgentLoop、Product/Workflow、权限、预算、事实源或默认模式。

## 设计边界

可委派问题必须同时满足：

1. 能只依据任务开始时的原始只读来源回答；
2. 不依赖主方尚未提交的修改或另一个助手的结果；
3. 能写成有界的 goal、briefing 和 evidence deliverable；
4. 主方可以同时处理任务的另一个部分。

简单任务和必须沿同一状态链连续推理的紧耦合任务留给主方。说明不得按文件名、领域词或测试题名称决定委派，不强制每个复杂任务创建助手，也不把助手报告当作已验证事实。

唯一拆分规则文本事实源为 `traceh.supervision.delegation.DELEGATE_GUIDANCE`。它既是 Adaptive 主方专属 prompt section 的正文，也是该主方独有工具的 description；两个装配位置不得复制出第二份规则。Workflow 的 Adaptive 任务消息只带一条模式提示并引用该 system section，不复制判断条件。Single 没有该工具、没有模式提示且继续使用原 `CODING_PROMPT_IDS`，因此不会收到拆分说明。助手仍只收到显式 work message 和原来源身份。

## 验证顺序

1. 定向检查 Adaptive 工具说明包含拆分条件，Single 工具集合仍不含委派工具；删除关键拆分句时新检查应失败。
2. 冻结三种显式材料：两个独立源码主题、一条紧耦合源码链、简单版本读取。原始材料和合格参考产物先经现有无网络 Docker Verifier 做失败/通过反向预检。
3. 使用现有授权真实模型、原 EvaluationRunner/Product/Git/SQLite/Docker，仅运行新版 Adaptive 各一次；每请求不重试，不调用语义裁判或基线模型。
4. 可拆题要求成功 delegate、助手真实调用及 collect；紧耦合题和简单题要求不 delegate。任务成功、请求快照、不变量、Budget/Workspace 收敛分别报告，不能用一个指标代替另一个。

真实结果只与 2026-09-11 相同可拆/简单旧观察作描述性参考，不宣称统计显著性。首个“判断后可选委派”的工具说明被模型忽略后，可依据已冻结请求与工具轨迹在同一 `DELEGATE_GUIDANCE` 边界内收紧为条件动作规则；若该说明仍被忽略，可将同一事实源接入 Adaptive 专属 prompt section 后重新冻结独立观察。不得借此修改题目、Loop 或评分器。最终候选若仍未产生合理委派，恢复旧文本并保留失败；若产生简单题滥派，同样不采用。无论结果如何，不自动提交、发布或切换默认 `single`。

## 门禁

只运行相关定向测试、compileall、修改范围 Ruff、collect-only、反示例硬编码、请求重放与文档检查。不得运行全量 pytest、L2–L4、Wheel、旧 72 题或语义裁判；真实调用必须先冻结材料与上限，连接失败不重试。

## 执行结果

五轮候选依次覆盖工具说明、条件动作、Adaptive system section、明确分类/下一动作及 Workflow 任务消息提示。15 个 Product trial、121 次真实 qwen-plus 调用均未产生自然委派；最后一轮还出现可拆题失败和紧耦合题达到本地调用上限。候选全部拒绝，生产 `DELEGATE_GUIDANCE`、Prompt 装配和任务消息已恢复到实验前状态。

离线副本重开 45 个 Session 并重放 153 份请求，请求重建与不变量无错误；所有 Budget 收敛、Workspace live=0。下一阶段若继续，应另立 typed decomposition decision 合同，不在本合同继续叠提示。详见[记录 032](../deal/032-adaptive-decomposition-guidance.md)和[派生汇总](../validation-data/dynamic-collaboration/adaptive-decomposition/README.md)。
