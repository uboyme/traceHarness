
# 记录 074：single 与 multi 的同题配对对照

2026-09-14。用户授权在记录 073 之后做收益对照。本轮第一次回答"多助手到底值不值"。

## 方法

复用仓库自己的 `execution_strategy` 配对机制，不另造评分：一份 run-plan 两个臂，baseline=`single`、candidate=`multi`，由 `traceh eval` 一条命令先后执行，最终判定由原 `comparison` owner 给出。驱动 `tests/live_dynamic_collaboration/strategy_comparison.py`，题目沿用记录 073 的五模块流水线（`autonomy_materials`），requirement **不写数量**。

公平性：两臂共用同一份冻结 Profile、同一套固定检查、同一个固定镜像、同一连接与模型（deepseek-v4-flash），同一批上限——任务 1500000 tokens、coder 1200000、每助手 50000、进程槽 root 5 / coder 4、单 Turn 600 秒。差异**只有 requested_mode**。每臂 1 个复本，`first_arm=baseline`。预检在固定镜像内证明空实现 exit 1、参考实现 exit 0。

第一次尝试因 run-plan 的 `base_url` 为空被 CLI 在任何模型调用之前拒绝（**零 token 消耗**），修正后重跑。

## 结果

| | single | multi | 倍数 |
|---|---|---|---|
| 固定检查 | passed | passed | 持平 |
| exact tokens | 50902 | 315438 | **6.2×** |
| 工具调用 | 13 | 46 | 3.5× |
| 模型调用 | 11 | 32 | 2.9× |
| 墙钟 | 62.7 秒 | 199.8 秒 | **3.2×** |
| 收敛/不变量 | converged / passed | converged / passed | 持平 |

multi 臂再次**自主拆出 4 个 assignment**（`telemetry_parse` / `telemetry_rules` / `telemetry_format` / `telemetry_stats`，各一个文件），4 份 Patch 全部捕获并成功整合，固定检查**一次通过**——这是三次观察里第一次功能也过关。

原 comparison owner 的判定：**`status: regressed`**，`changes: {gain:0, loss:0, unchanged:1}`，`cost_delta: {total_tokens: +264536, tool_calls: +33}`，`quality_status: no_change`，`hard_constraints: passed`，`adoption_authorized: false`。即：**质量没变、成本大涨，记为回归。**

## 读数

在这道题上，多助手**不划算**：同样通过，代价是 6.2 倍 token 和 3.2 倍时间。原因在数据里看得很清楚——四个助手合计只花了很少的 token（每人约 2–3 万），贵的是主方：它要读完 4 份补丁、做 4 次整合、再自测，上下文随之膨胀。**"整合税"压过了并行收益**，因为每个模块只有几十行。

这也解释了记录 073 第一次为什么会被自己的 token 拖垮：拆得越多，主方越贵。

## 边界

一题、每臂一个复本、一个模型，这是**对这一类任务**的观察，不是普遍结论，也不是统计量。它不否定多助手机制本身（本轮 multi 臂功能通过、机制全链成立），只说明：**在单元很小、整合成本相对高的任务上，多助手是净亏。** 要证明多助手有收益，需要的是"单元足够大、整合相对便宜"的任务类型，以及多个复本；这属于后续独立实验，本轮不追跑。

精简证据见[对照证据](../validation-data/dynamic-collaboration/strategy-comparison/summary.json)。
