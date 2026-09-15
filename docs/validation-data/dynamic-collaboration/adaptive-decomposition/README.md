# DA-6 Adaptive 拆分提示实验

本目录只保存精简、可审计的派生汇总。原冻结合同、材料、断网 Docker 预检、Product 报告、SQLite Session/Effect 和请求快照位于本机 `.traceh/da6-real-1` 至 `.traceh/da6-real-5`；这些运行目录不进入版本库，也不包含在本文件中重写的事实源。

五轮依次测试工具说明、条件动作说明、Adaptive 专属 system section、明确分类规则和任务消息模式提示。每轮均使用同一三类题、同一 qwen-plus 连接、一次请求一次尝试、原 EvaluationRunner/ProductTaskEvaluator、原 Git/SQLite/Docker 主线；题目不出现 `delegate_investigation` 或“必须委派”。

15 个 Product trial 共调用真实 Provider 121 次，产生自然委派 0 次。所有五轮的简单题都未误派；候选没有证明目标能力，且最后一轮出现任务退步。因此所有候选均拒绝，生产 `DELEGATE_GUIDANCE` 恢复到实验前文本，Adaptive system section 与任务消息提示均撤回，默认仍为 single。

离线复核在临时 SQLite 副本中重开 45 个 Session，重放 153 份请求，请求重建和 CoreInvariant 均无错误。121 次真实 Provider 调用加 30 次零用量本地 requester 的成功记录合计 940,445 exact tokens；另有 2 次本地调用上限拒绝发生在 Provider 调用前。

候选撤回后的最终定向集合为 31 项通过；compileall、修改范围 Ruff、全仓 4025 项 collect-only、反示例硬编码、文档与 diff 检查通过。未运行全量 pytest 或 L2。

完整解释见[记录 032](../../../deal/032-adaptive-decomposition-guidance.md)，执行边界见[合同](../../../plan/TRACEHARNESS_DA6_ADAPTIVE_DECOMPOSITION_CONTRACT.md)。
