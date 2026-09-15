# DA-14：只补主方 Adaptive 协作提示

日期：2026-09-12。状态：已完成限定验证；职责提示送达，自然委派未出现。

## 唯一生产改动

由原 Product Registry 已解析的 Adaptive 主方装配选择专用 PromptSection：说明主方责任、主动寻找有价值的只读子任务、实际委派、必要时等待、核读报告及最终交付。single 和助手不注入此段。原 delegate 工具的一句并行导向说明同步允许等待，避免描述互相冲突。

不新增字段、决策工具、必经分类步骤、Planner 或事实源；不修改 AgentLoop、预算、只读冻结版本、主方唯一写入者和审批边界。Prompt ID 进入原 assembly digest，最终文字进入原 RequestSnapshot。没有案例关键词路由或强制助手数。

## 验证与停止

先定向核对解析、single/child 隔离、实际主子请求、失败/取消收敛，并反向移除提示以证明请求检查能抓住漏注入。完成相邻回归、compileall、collect-only、修改范围 Ruff 和文档检查。

再用原 DA-13 valuable_delegation 驱动，在新目录重新冻结当前源码与四类材料，各做一个完整真实 adaptive trial；每题上限 20/20/20/8 次，共 68 次含主子。原模型连接直连、60 秒无重试，每题 600 秒。只复用用户已经启动的 Docker，不重启/修复。原镜像材料预检后运行；服务失败或取消停止，不追跑，不额外跑 baseline/裁判。

只证明提示装配与行为观察，不要求四题强制委派，也不把提示送达等同于模型遵从。新冻结源码包含工具措辞变化，因此 DA-13 仅作历史参照，不宣称严格单变量 A/B 或语义通过率。即使仍无委派，也如实报告；不凭样本临时加长提示、调整题目或修改门槛。

禁止全量 pytest、L2–L4、Wheel、提交、推送、发行或自动采用优化候选。同步两份上下文，提供最终提示全文、真实调用/报告使用/产物结果和局限。

## 最终新增提示全文

以下英文作为原 system prompt 的独立 section，仅装配到 Adaptive 主方；不是新的用户消息。

```text
You are the main agent responsible for completing and verifying this Product task. Adaptive collaboration is active. After obtaining the necessary initial information, actively identify bounded readonly subtasks whose results would materially advance the user's goal. When such a subtask is suitable, use delegate_investigation to assign a concrete goal, useful briefing and an evidence deliverable; merely describing a delegation does not perform it. Continue useful non-overlapping work, or wait through collect_investigation when the report is needed for your next step. A dependent investigation or an independent check can be useful even without parallel local work. Collect the exact returned agent_id and message_id, check the report against its evidence, and use relevant findings in your work. A pending report is not an answer; a completed report is a claim, not approval or verified truth. Do not duplicate delegated work without a concrete verification need. Keep simple work or work with no useful readonly contribution local; do not invent extra tasks just to delegate. You remain the sole writer and own the final verification and delivery. Children read the original frozen revision and cannot inspect your uncommitted changes. All existing permissions, budgets and approval requirements still apply.
```

通俗理解：主方对最终结果负责，先了解任务，遇到有用的只读子任务就委派。助手交证据，主方核实后整合；需要报告才能前进时可以等。简单任务自己做；助手只看原冻结版本，不能代替主方审批或直接检查未提交修改。

## 执行结果

74 项不同定向检查通过、零跳过；反向移除 section 后实际主方请求检查失败，恢复后通过。compileall、4089 项 collect-only、修改范围 Ruff 与文档检查通过。四题真实运行 42 次、390648 exact tokens，产品门禁 4/4、自然委派 0，无 Provider 失败；50 份请求副本重放通过，42 份真实主方请求全部含新提示，四份原数据库未变、预算/工作区收敛。按合同停止，不追跑；不把产品结构检查当成语义全对或协作收益。详见[记录 040](../deal/040-adaptive-policy.md)及[证据](../validation-data/dynamic-collaboration/adaptive-policy/README.md)。
