# single 与 multi 的同题配对对照证据

`summary.json` 对应[记录 074](../../../deal/074-strategy-paired-comparison.md)，使用仓库自己的 `execution_strategy` 配对机制（run-plan 两臂，由原 comparison owner 判定）。

两臂共用同一题目、同一固定检查、同一镜像、同一模型与同一批上限，只有 requested_mode 不同。结果：两臂都通过固定检查；multi 花掉 6.2 倍 token、3.5 倍工具调用、3.2 倍墙钟，判定为 **regressed**（质量无变化、成本上升，`adoption_authorized: false`）。

multi 臂在题面不写数量的情况下仍自主拆出 4 个 assignment 并成功整合 4 份 Patch，所以这不是机制问题，而是**整合税**：助手每人只花 2–3 万 token，贵的是主方读 4 份补丁、做 4 次整合并自测。

边界：一题、每臂一个复本、一个模型——这是对**这一类任务**的观察，不是普遍结论。原始运行目录保留在本机忽略目录。
