# 039：DA-13 完整真实任务观察

日期：2026-09-12。状态：四个完整真实任务已运行；Product 门禁 3/4、委派 0，未证明协作收益。

## 为什么调整

DA-12 的冻结标准研究独立并行分工，助手先取证、主方随后分析不满足当时标准，但不代表这种协作没有价值。本轮按用户要求接受依赖式调查、主方等待和针对冻结材料的复核。是否有贡献要核对报告及交付，不能由角色名称、同时开工或不同文件路径推导。

## 本次实现

- 新增显式实验驱动 `tests/live_dynamic_collaboration/valuable_delegation.py`，复用原 EvaluationRunner / ProductTaskEvaluator 与现有材料构建、预算封顶、直连 Provider 入口，没有另造评分器。
- 冻结可并行调查、依赖调查、冻结草稿复核、简单版本提取四题；每题一次 adaptive，上限 20/20/20/8 次真实调用，主方和助手共用封顶。题目不包含强制委派命令。
- 任务结果、报告实际送达、报告使用待审和贡献未证明分开记录。主方自己完成也合法，但标记协作链路未被验证。
- 修正新材料中的源码引用：当前延续方法是 `decide`，旧实验材料中的 `after_tool_results` 不再用于新测试。加强本轮验证器，逐一检查所有章节的代码行引用，不能用 INDEX 或待审草稿冒充代码证据。语义正确性仍需离线核读。
- 输入摘要冻结与排他运行标记防止漂移/重跑；异常及取消留下停止类型，后续案例不继续启动。原生产代码、默认 single、助手只读、主方唯一写入者、预算/身份/事实源和人工采用均不变。

## 实际验证

30 项定向及相邻测试通过，1 项原真实 Docker 检查因未提供显式可用镜像/上下文跳过。覆盖四题材料与验证器、错误引用、输入漂移、任务失败、报告未送达、无助手、等待、驱动异常/取消与禁止重跑，以及原材料、协作诊断、调查工具和预算回归。

反向移除本轮“检查所有章节引用”后，第一节越界引用反例按预期失败；恢复后通过。compileall、4087 项 collect-only、修改文件 Ruff、文档链接/章节/围栏与 diff 检查通过。案例词仅存在于显式实验材料，没有生产案例分支。

## 真实执行与结果

最初 Docker 初始化遇到残留套接字，按一次有界恢复停止；零调用记录保留在 environment-block.json。随后用户手动启动 Docker，直接查询引擎成功（29.2.1），冻结摘要保持不变，没有再重启或修 Docker。四题各在原固定镜像中验证缺交付失败、参考结构通过，共八次真实容器预检通过，然后每题只运行一次。

| 显式实验题 | 原 Product 门禁 | 真实调用 | exact tokens | 助手 |
|---|---|---:|---:|---:|
| 可并行调查 | 通过 | 10 | 92087 | 0 |
| 依赖式调查 | 失败 | 10 | 71836 | 0 |
| 冻结草稿复核 | 通过 | 12 | 123864 | 0 |
| 简单版本读取 | 通过 | 5 | 20693 | 0 |

共 37 次 qwen-plus 直连、308480 exact tokens，无 Provider 失败、无重试、无调用封顶。所有 37 份主方请求都可见 delegate/followup/collect/stop 工具；模型全程独做，四题 chain_observation 都是 not-exercised。没有报告可供评价使用或贡献，也不能宣称等待式或并行式协作已经成功。原 Product 门禁主要验证交付结构与不改原文件，不是独立语义评分。

## 失败根因与语义核读

依赖式调查已经读到目标文件两页原文，但新建 answer.json 的 apply_patch 调用遗漏 create=true。当前路径解析在必须存在的模式下找不到该文件，返回 WorkspaceBoundaryError。模型没有补 create=true，而把 `echo '...' > answer.json` 交给按 argv 执行的 shell 工具；原 effect 记录的 argv 含独立的 `>` 和 `answer.json` 参数，stdout 只打印文本，publication.applied 为空。没有产生文件或补丁，工作流验证节点以 promotion-patch-invalid 失败。这是任务真实失败，不是连接失败，也不能用模型构思过答案来算通过。

实现 Agent 离线核读了三份实际推广到隔离目标仓库的 answer.json：

- 可并行调查：账本投影和 delegate/followup 生命周期基本方向符合源码，但内容简略，collect 的结论缺少直接实现引用，不等于完整语义满分。
- 草稿复核：正确否定“请求后自动获批并继续”；delegation.py 320–321 明确说明 grant 不启动执行、应显式 followup。另两处引用只是工具名常量，可以更扎实。
- 简单题：0.11.0 与冻结 version.py 和精确验证一致。

这些说明保存在 semantic-notes.json，属于实现 Agent 的观察，不是独立裁判、用户批准或正式语义通过率。没有在看到结果后修改门槛、修提示或追跑。

## 证据与边界

八个 Session 的 45 份请求在原日志副本中独立重放通过，CoreInvariant 无错误：37 份真实执行请求与 8 份产品流程的本地控制请求分别计数，不把后者多算为远端调用。四个原数据库 SHA-256 未变，四题 Budget、Workspace 都收敛，live=0、无隔离待清理工作区。原报告、失败 effect、CAS 产物与隔离 Git 仍保存在 `.traceh/da13-real-1`，离线复核位于 `.traceh/da13-audit`。

隔离 Product 实验内部仍走原程序化批准及目标 Git 推广，用来验收完整任务；这不代表用户项目代码获准提交或采用。没有改生产、强派助手、启用新 Planner、重建事实源，没有 full pytest、L2–L4、Wheel、baseline 或独立语义裁判，也没有提交、推送、tag、发行。用户启动的 Docker 保持运行。

正式与通俗上下文同步了当前状态、12.22 的真实结果、14.1 与 15。前一轮 30 passed / 1 skipped、compileall、4087 项收集、Ruff 和反向检查仍作为未改代码的验证记录；本轮额外完成真实预检/完整任务/重放，并重新核对冻结输入、文档、JSON、链接、围栏和 diff。见[合同](../plan/TRACEHARNESS_DA13_VALUABLE_DELEGATION.md)和[完整证据索引](../validation-data/dynamic-collaboration/valuable-delegation/README.md)。
