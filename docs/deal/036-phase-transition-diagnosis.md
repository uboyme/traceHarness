# 036：DA-10 阶段切换请求诊断

DA-10 已完成[冻结的四条件对照](../plan/TRACEHARNESS_DA10_PHASE_DIAGNOSIS.md)。结论是：**移除旧侦察提醒、末尾重申决定指令，两项单独或同时使用，都没有修好第一步决定行为。** 本轮结束后没有继续改提示追跑，也没有恢复 DA-9 生产候选。

## 实际查到的编排链

原 DA-9 候选通过 Product 的 AdaptiveDecompositionPhase 修改当前 Composition，决定阶段的 system 确实包含“只能调用决定工具”，tools 也确实只有 decide_task_decomposition。OpenAICompatibleProvider 把这两项发送进 messages/system 和 tools；本阶段用真实 Provider 实现的传输替身测试核对序列化，没有发现客户端丢掉它们。

同时存在另一个信号：候选在第一次侦察后用 Continue.messages 追加侦察提醒。现有 [AgentLoop](../../src/traceh/runtime/agent_loop.py) 将它写成普通 user/message；[SurfaceProjector](../../src/traceh/session/surface.py) 继续投影该消息，没有阶段失效含义。[Context Input](../../src/traceh/session/context_input.py) 在最后的参考包再引用原始用户目标并强调完成原任务。因此“总任务”“当前步骤”“过去的宿主提示”在呈现上并未充分分开。

这些是已核实的请求事实，但“它们导致了全部失败”仍是假设。当前实验也没有删掉最终参考包或重写总任务。不能因怀疑参考包而撤掉已有换题保护，更不能按字符串匹配在生产中删除历史。

## 怎么隔离变量

从三类原场景各取第一次进入决定阶段的 dispatch_request。四个条件分别是原样、只删恰好一条旧侦察提示、只在末尾追加相同决定 system 指令、两项同时做。所有读取正文、assistant/tool 配对、用户原目标、参考包、工具 schema、模型和参数保留。

末尾重申会同时改变位置和重复次数，不能单独归因于位置。这是单步请求探针，不是新的任务评测框架：只调用原 Provider，结果作为未执行提案保存，不走 ToolRuntime，不创建助手，不写工作区，也不把修改后的诊断请求冒充原 Session 的请求。原请求身份和摘要明确单列。

## 真实结果

qwen-plus 直连，每个条件每场景一次，没有失败重试或追加样本：

| 条件 | 有效决定提案 | 精确 tokens |
|---|---:|---:|
| 原样重调 | 0/3 | 11092 |
| 只删旧侦察提醒 | 0/3 | 10683 |
| 只在末尾重申当前决定 | 0/3 | 11556 |
| 两项同时做 | 0/3 | 11146 |

共 **12 次真实调用、44477 tokens、0 次 Provider 失败**。两个复杂场景在四个条件下都继续请求 read_file；简单场景继续请求 apply_patch 或 write_file。没有一次提交 decide_task_decomposition，因此无法进一步比较 local/separable 是否正确。原 DA-9 的第一次决定也都是 0/3；本轮没有重新跑完整 Product 任务，不能将这里的 0/12 当作任务成功率或与历史 1/3 直接相减。

模型回复表现为继续原任务：看过索引就想读源码，看过版本就想写文件。这支持“模型延续已有执行轨迹”的行为描述，但没有把模型能力、历史惯性、长提示干扰和服务端工具遵循能力拆成独立原因。特别是可拆题依旧只读到索引，原证据充分性限制仍存在。

## 验证、边界和下一步

22 项新增定向测试及 Provider、Context、原实验材料相邻回归共 **75 passed，0 skipped**。移除转换操作后，三个干预条件都因预期差异消失而失败，恢复后通过。测试覆盖证据配对、原请求不变、输入缺失/歧义/摘要错误、非法提案、原 Provider 序列化、失败和取消传播、未知 usage 停止、12 次上限及同目录拒绝重跑。compileall、4047 项 collect-only、修改范围 Ruff、文档与 diff 检查通过。

三个原 SQLite 摘要在准备、调用前和调用后保持一致；12 份派生请求及原始响应保存在独立诊断目录，调用前先写请求，失败不打印原始错误或密钥。精简数据见[验证索引](../validation-data/dynamic-collaboration/phase-transition/README.md)。本轮没有全量、L2–L4、Wheel、Docker 启动、完整任务基线或语义裁判；没有提交、发布、生产改动或 Promotion。

这次经验是：**修正提示的表面顺序不等于模型会改变当前动作。** 保留事实并明确下一步，仍需验证模型是否真的遵循；不能只验 system 含某句话就宣称问题解决。

若继续，值得单独冻结“独立决定输入”的实验：保留原目标和带来源的侦察证据，在没有先前执行轨迹的决定调用中比较行为，用来区分续写惯性与决定本身的困难。它尚未实施，不能先增加另一个 Planner 或新的状态机。当前保持原 adaptive、默认 single，DA-9 仍撤回。
