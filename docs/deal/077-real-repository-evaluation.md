# 记录 077：真实仓库 Issue 评估接入

2026-09-20。执行合同见[计划](../plan/TRACEHARNESS_REAL_REPOSITORY_EVALUATION_PLAN.md)。本文件持续记录观测、决定和验证，
正式能力以源码及正式上下文为准。

## 已发生的工作

### RR-0：启动与基线

- 用户授权开启目标模式，完成真实任务接入，允许必要基础设施修复并要求保持原架构。
- 已建立目标；工作区初始 `git status --short` 为空。
- 已核对 Evaluation/Product 材料加载：初始树使用有界普通文件快照，自建一次性源仓库与 bare target。
- 启动时生产上限为 256 文件、单文件 1 MiB、总 8 MiB；同一 Product suite 最多 16 题。此处记录旧基线，不决定新上限。
- Docker 首次 `version` / `image ls` 返回 Linux engine named pipe 不存在；Python 为 3.12.7。
- Docker Desktop 已启动，server 为 29.2.1；还没有模型调用或真实任务得分。

### RR-1：公开材料和排除记录

- 下载 SWE-bench Lite dev 的固定 dataset revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。
  dev parquet SHA-256 为 `8312f321838051849d2fa7c6ca071244733a3e90bb517ff6ca8186f722199b5c`；
  逐行核对与查看器的 23 条记录一致。使用 dev，不将公开题称为未见过的留出集。
- 候选覆盖序列化、数据格式处理和静态推断：marshmallow-1359（78 文件、720107 bytes）、
  pydicom-1694（478 文件、9380936 bytes）、astroid-1196（272 文件、2030097 bytes）。
  均下载 base commit 的完整源码压缩包；三个候选没有链接或特殊文件。选择发生在模型运行之前。
- 排除 sqlfluff-1625：所附新测试只验证提示文本，不能充分裁定问题陈述中的别名行为。
  sqlfluff-1733 源树含 symlink，目前 Workspace/Sandbox/Promotion 合同均不接受；不删文件后冒充完整仓库。
- 这三题是接入材料，不是统计样本量设计；未改变既有单/多智能体收益结论。
- 环境准备：Python 3.10.18 容器层下载完成后，Docker registry 返回 referrers index HTML 解码失败，
  首次 pull 退出 1。显式 linux/amd64 和完整 registry 名重试成功；未跳过 TLS 或执行宿主依赖安装。
  专用依赖镜像已构建，ID 为 `sha256:4aa70636a73796d1fa51b3f26dfa52a402b088d8f6707a3eef3a000c4ddf34fe`。
  依赖版本与构建命令见 `benchmarks/real_repository_v1`；运行期 network=none。
- 已逐项核对固定上游 Git tree API 清单：78/478/272 个 blob 的路径集合与 archive 完全相等，
  每项 Git blob SHA、文件长度均一致。不存在被 export-ignore 遗漏的文件。原 mode 记录在清单中，
  当前 Windows 初始树协议不重建 POSIX 可执行位；上游 base commit 与本地确定性源提交分别记录。

### RR-2：初始树 owner 的最小调整

- Product dataset 切换到 format 3，每题必填 initial_tree_limits（文件数、单文件 bytes、总 bytes）。
  捕获、冻结和每次 attempt 复制都消费同一份额度，不从模型、题号或 PatchCaptureLimits 猜测。
  根 Evaluation 协议仍为 3，其他类型 dataset 不变；旧 Product dataset 2 明确拒绝。
- 完整材料建仓时显式 force-add 已冻结文件，防止原仓库已跟踪但匹配 ignore 的文件被重新建仓遗漏。
- 普通文件、链接拒绝、材料摘要、独立 bare 目标、预算、取消、审批和 Promotion owner 不变。
  同步维护中的测试材料生产者和 shipped 数据；不自动迁移用户已有运行目录。

### RR-3：已完成的实际材料与原主线验收

| 任务 | 原版通过 / 目标失败 | 参考通过 | 缺失、跳过、setup 错误 |
|---|---:|---:|---:|
| marshmallow-1359 | 76 / 1 | 77 | 0 |
| pydicom-1694 | 26 / 1 | 27 | 0 |
| astroid-1196 | 24 / 2 | 26 | 0 |

- 首轮 marshmallow 准入没有放行：8 个 PASS_TO_PASS 标识被历史日志按空格截断，虽然 76/77 个 pytest
  节点实际执行，精确 ID 检查仍判缺失。现在 selection 显式冻结这 8 个完整 ID 映射；生产不做前缀猜测。
  原始 dataset 行保持不变。最终三题 6 次准入均通过，固定报告在 `.traceh/rr-eval/final-admission/`。
- `.traceh/rr-eval/final-reference/`：三题通过原 EvaluationRunner/ProductTask 的真实 Git、固定 Verifier、
  Review、隔离目标审批和推广。3/3 completed、Review passed，6/6 预算账户关闭，6 工作区 released、live=0。
  report.json SHA-256 为 `ec2bc73b948fe008e87b18a14347f369e57b733fee73621faa631b72e0fc5899`。
- `.traceh/rr-eval/unfixed-v1/`：三题分别真的改动源文件但保留原缺陷，均产生 failed Review、无 Approval/Promotion。
  6/6 账户关闭；3 工作区 released、3 dirty 工作区 quarantine，live=0。
  report.json SHA-256 为 `0a1c857aef38213e6534233d5a70294dbf28c1acf2c724ea9cf663c40f6a5447`。
- 以上均为显式离线 answer injector，模型调用为零，不能当作自主解题率、真实 token 成本或模型排行榜。
- 原 `load_run`、Session invariant/request replay 与 Product metric reader 已独立重读正负两组：
  6 个 attempt、24 份请求快照、实际目标 ref 和全部派生 evidence 一致；SQLite 文件摘要前后不变。
  [可审阅摘要与环境](../validation-data/real-repository-v1/README.md)保留结果和绑定；原完整证据留在上述工作目录。
- 两个生产保护已反向验证：去掉 force-add，bare 目标实际缺少已冻结 ignored fixture；去掉文件数门禁，
  超额捕获不再拒绝。固定测试完整性也做了反向验证：关闭哈希保护后，篡改测试被隐藏测试覆盖并假通过，
  新单测随即失败；已恢复原保护，22 项材料/建仓定向测试通过。

### 回归中发现并记录的问题

- 第一次广域定向回归发现维护中的 Product 测试生成器仍写 format 2、示例 run plan 仍绑定旧 manifest
  SHA。已同步格式与摘要，旧协议拒绝测试继续保留。
- 显式打开 Docker 后，旧 `test_product_adaptive.DelegatingProvider` 仍读单个 `child`，与生产 `children`
  合同不符。已在独立干净 HEAD 源码副本复现同一失败（解释器导入路径指向副本），并更新本轮依赖的夹具。
  旧 multi benchmark 夹具还授权助手运行 120 秒，却使用仅允许等待 55 秒的 await_report；在干净 HEAD
  独立复现。现将该小夹具的整次助手 grant 明确设为 30 秒，与原单 Turn 上限相符，未放宽生产准入。
  上述相关 child 正/反例、multi 整链、single/multi 同条件比较 4 项复测通过。
  close-error 合并测试的手工 task 替身补齐新必填额度，确保仍进入原失败/关闭路径；23 项材料/建仓/关闭测试通过。
  当时其余相邻回归尚未收尾，最终结果见下方收口记录。
- 检索 shipped baseline 测试的原断言是 11 次 single，但当前题库已含 single/multi；干净 HEAD 的公开
  EvaluationRunner 直接展开 22 个 trial，确认旧测试条件已漂移。该测试现在显式传入
  `RunOptions(requested_modes=("single",))`，沿原筛选入口验证预期的 11 查询，不修改 shipped 双模式题库。
  较早启动的广域批次已导入旧夹具，保留其失败记录；修复后的相应文件另行完整重跑。
- 修复后 Product/Adaptive/Optimization 文件组为 27 passed、1 failed；实际助手已完成并进入公开
  `TaskConversationReader.load`，失败为调查目标未格式化。定位到只读展示仍只识别 format 2，而当前
  `readonly-investigation` 写入/校验协议已是 3。属于已有 TUI 投影遗漏，未涉及执行权限或持久化状态。
  已补读相邻合同；在冻结源码的在途回归结束后，将该展示分支切到当前 format 3，保留原反例。
- 旧夹具广域批次现已结束：200 passed、11 failed（2409.38 秒）。失败分布为旧助手报告字段 1、
  旧 multi grant/await 条件及依赖它的 Product/AO/可写评估 8、手工 task 缺额度 1、检索模式分母 1。
  原日志和 JUnit 留在 `.traceh/rr-eval/regression.*`，不删除失败或将该批次称为绿色。
- 检索/失败/UI 优化/委派/可写组复跑为 35 passed、3 failed（1075.67 秒）。检索的 11 次 single 已全部通过。
  可写评估三项在原 grant 修正后暴露下一处旧夹具：`WritableProvider` 仍断言工作信封 format 1，导致实际
  format 2 被测试 Provider 抛为异常。独立干净 HEAD 的公开 Product 路径复现同一失败（15.38 秒）；
  已只修共享夹具到当前 format 2，追加回归覆盖正向整合、冲突/未读拒绝、失败与取消收敛。
- 最终 Product adaptive、可写评估、可写协作、补丁整合、任务对话和 presentation 六文件组：
  **61 passed、0 failed、0 skipped，525.50 秒**。此前所有失败均有后续 owner 复测通过。
  四份 JUnit 按节点合并后覆盖 269 个不同测试，最新结果均 passed；不将此描述为一次全量绿色。
  [批次摘要](../validation-data/real-repository-v1/regression-summary.json)记录各原始文件的 SHA-256 与失败分布。
- 最终 compileall 通过，collect-only 为 4311 项；修改范围 Ruff 与 diff check 通过。
  材料/建仓与 Evaluation/Product/Promotion/Artifact/Workspace 架构保护合计 58 项通过。
  文档本地链接存在、围栏闭合，新增材料摘要一致；未发现凭据形态。
- 本轮未运行全量、Wheel、L2–L4、外部模型；不把未配置 Docker 导致的 skip 作为真实执行通过。

## 当前证据与未完成项

- 已验证：完整上游材料、原始缺陷、参考修复、原 Product 整链、错误修复拒绝、资源收敛。
- 已收口：定向及相邻回归、原证据重读、compileall、4311 项收集、修改范围 Ruff、diff check 和文档门禁。
- 未运行：全仓完整测试、发行门禁、真实模型对照；三题仍只是公开开发材料，没有统计收益结论。
- 未修改：执行策略、评分器、模型配置和 Git 历史。
- 文档同步：正式/通俗版 12.5；只读展示补正为正式 20.36 对应通俗 20.30；入口、导航、题库说明、Changelog 同步。
- 后续：本阶段离线目标完成；真实模型试跑仍须明确模型、总费用额度和停止条件，不能用参考答案验收顶替。
