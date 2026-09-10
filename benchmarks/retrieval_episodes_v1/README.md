# 独立检索旅程开发题库

根协议 3，task_type=retrieval_episode，经唯一 `traceh eval` 运行。
这是原主动检索实验的 24 个模板 × 3 组材料，共 72 条开发/回归材料，已被多轮诊断使用，不是未见留出集。
原 55/72 是历史合成口径，不是这个入口的新成绩；原 `retrieval_v1` 继续测 Product 内 F5 指标。

`dataset.json` 冻结问题、四种准备配方和宿主 expectation；`materials/` 是明确脚本/资源原文。
具体故事、ID、数字、种子和脚本名仅是本题库材料，没有进入通用运行器的隐藏默认值。
模型工作区不包含 dataset、rubric 或报告。Output 脚本须经显式宿主沙箱执行，禁止目标轮重跑。
Memory 使用真实批准/替代/撤销，并绑定独立读取会话；Skill 使用原 Plugin/Generation；History 使用真实回合与压缩。

复制 `run-plan.example.json` 到自己的配置目录后，填写实际模型连接和 sandbox_config。
示例显式选择种子 113 的 h-direct/h-absent/s-direct/s-absent/m-direct/m-revoked/o-direct/o-absent 八条，
max_trials=8；它是 UE-2 主线验收，不能据此推算完整题库得分。
`paired-run-plan.example.json` 是同样八条的 UE-3 双臂 A/A 示例：两臂均为 current，max_trials=16，
显式 direct 网络与关闭期限。它不是系统默认，也不是新的 72 题测分；改为文本候选前须按
[UE-3 合同](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_CONTRACT.md) 提供确切源码和节点摘要。
每臂单独 review/assess，再用离线 --compare 与明确的 --assessments 引用比较；没有人工判断继续待审。
修改材料后需重新冻结引用摘要；Git 按原始字节保存本目录，不执行换行转换。

每条运行先给 provisional 检查和 pending_review。使用 --review 导出证据、--assess 导入明确人工判断，原运行不变。
UE-3+ 在同一出口写 diagnostics.json/md，展示相关来源候选、足够证据派发、原评估状态及读取/搜索范围。
可直接对关闭后的旧运行离线导出，不调用 Provider；未执行项仍保留，目录或 accepted 回执不算正文。
负例需要核对否定范围，诊断不自动判通过。见 [诊断合同](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_PLUS_CONTRACT.md)。
完整字段、命令、判据与边界见 [UE-2 合同](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE2_CONTRACT.md)。

UE-4 按原题目/材料/预算新跑当前单臂 72 条，正例暂定联合通过 49/60，12 条负例仍需范围审阅，正式评分全部待人工。八条普通/多来源对照单独报告，不并入分母；见 [真实证据](../../docs/validation-data/unified-evaluation/ue4/README.md)。本题库已用于开发，不能称作未曝光留出集。
