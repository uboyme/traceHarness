# WC-4 收尾证据修复与第二次有界验收

2026-09-13。用户在核对记录 054 的失败后，明确同意修复分工、断言式验证、收尾证据，并另开一轮同样上限的真实验收。本轮代码与定向门禁完成，但真实完整验收仍失败。旧轮没有被覆盖，新轮结束后没有追加调用。

## 实现及边界

- 在原 writable 工作信封的 deliverable/briefing 说明中要求可检查的输入、输出、错误、修改范围及主方验证责任；不增加协议字段，不写死业务示例。
- 主方收尾要求用断言或非零退出表达检查失败，禁止额外文件时使用现有 Shell 的内存命令；助手仍不获得 Shell、网络、递归或 Promotion 权限。
- WorkspaceService.inspect_session 在原生命周期/编辑锁内重新核对 Agent/Session 并提供受管 handle。Product 的 WorkspaceDeliveryReader 使用原 GitPatchBuilder、原捕获上限及两次稳定快照，读取当前 Git 可见变更清单、candidate tree 与 Patch 摘要。它不写 Artifact/CAS/Approval，不改变索引或工作文件；Git 构建器仍会生成临时索引及候选 Git 对象。忽略文件不在该清单，不能据此声称整个磁盘没有变化。
- 原 StepViewPolicy.select 改为异步，当前实现和测试调用者一起更新。每次 review 请求前读取该观察结果，随原 request/view、request/snapshot 冻结；重放读取冻结记录，不访问后来的工作区。原最近八条工具回执保留，delivery 只提供观察，不生成范围或语义通过结论。非受管测试入口显式显示 unavailable；生产 multi 必须绑定原 Workspace 和 Capture 上限。
- 没有新运行器、可变验收账本、权限扩大或自动修复循环。观察失败/取消沿原请求与 Git 子进程收敛；固定 Verifier、人工批准和 Promotion 仍独立。

## 定向验证

请求视图/协作流程 13 passed；新增文件观察、错误 owner、取消后锁释放及断言式收尾 7 passed；相邻架构、只读/可写助手、Registry/Assembly 74 passed；原 Git Patch、Workspace 编辑与冻结合同 27 passed、1 skipped（平台符号链接能力）。另一个 Evaluation/收尾集合 5 passed，集合有重叠不相加。

两个独立文件名的公开 Product 用例证明：第一次收尾看到已改文件；再新增文件后，下一份请求观察到新路径和新 candidate tree；原请求重放通过。故意抹掉真实观察的 changed_paths 后，该公开路径失败；未改源码，用独立进程替换返回结果，退出后恢复原实现。实际 shell 断言不成立时 exit 1，成立时 exit 0，均未被回执标为语义通过。

compileall、修改范围 Ruff、collect-only 4186、git diff --check 通过。不跑全量、L2–L4、Wheel、安装或额外收益实验。

## 新轮冻结与结果

新证据目录 `.traceh/wc4-review-20260913/`。当前源码单独冻结；题目、权限、沙箱、初始文件和固定评分字节与记录 054 完全相同。预检仍为占位实现 exit 1、参考实现 exit 0。上限为主子共 32 次、600 秒、连接 60 秒、零重试、一题一次，不复用旧目录的 started 标记。

| 项目 | 观察 |
|---|---|
| 真实调用 | 19：主方 14、助手 5 |
| Exact tokens | 139444：主方 112613、助手 26831 |
| 耗时 | 223842 ms，约 224 秒 |
| Provider 失败 / 重试 | 0 / 0 |
| 子 Patch 原文 | 1776 字节，全文进入主方后续 10 份请求 |
| 明确整合工具 | **未调用**；主方用普通 apply_patch 复制了助手代码 |
| 新收尾观察 | 确实进入原请求，列出三个变更文件，包括 test_telemetry.py |
| 自测 | 脚本包含 36 个 assert；首次 exit 1，修正自测预期后 exit 0 |
| 最终验收 | 固定检查 exit 1，Product / Workflow failed，无 Promotion |
| 收敛 | 3 个账户 closed、21 个使用预留 settled；live 0，released 1、quarantined 2 |
| 副本重放 | 21 份请求通过（19 真实、2 脚本控制），原数据库摘要不变 |

有改善的是自测真正执行了断言：首次错误预期认为两个正数中仅一个大于零，断言失败；随后修正预期再执行成功。此修改属于模型自己的检查，不是更改宿主冻结评分。

但主要缺陷仍在。主方新增了题目禁止的测试文件，收尾请求已明确展示三个路径，它仍声称只修改两个指定文件。助手仍缺少顶层 list 校验；对保留代码离线核对，None 抛 TypeError，空 dict/tuple 返回 []，而规格要求 ValueError。主方未使用明确整合工具，不能用最终文件内容相似冒充整合回执。固定 Verifier 保留拒绝，未发生批准或推广。

结论：新观察机制和断言式测试路径有真实证据；**“提示加证据展示足以约束交付”的判断未获支持**。不能宣称三项问题已解决，也不能宣称 WC-4 完成。结构化输入验收、交付范围与必需整合若要成为硬门禁，需要另行冻结通用合同和失败处理，不能在本轮临时解析示例文件名或继续调提示追跑。收益未测量。

两版上下文同步第 1、14.3.5、15、16、Workspace 主题及模块导航；旧记录 054、旧源码/评分冻结和失败数据库保留。未提交、推送或发版。分享证据见 [summary.json](../validation-data/dynamic-collaboration/writable-review/summary.json)。
