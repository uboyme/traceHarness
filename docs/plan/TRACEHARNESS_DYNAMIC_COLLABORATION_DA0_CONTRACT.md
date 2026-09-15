# DA-0：按需只读协作与执行策略对照合同

状态：执行与边界合同已冻结；材料和调用额度见 [真实实例合同](TRACEHARNESS_DYNAMIC_COLLABORATION_DA_REAL_CONTRACT.md)。A/A、机制与开发共 68 个 trial 已结束，DA-5 工程收口通过；一次真实提案 no-candidate，基础策略未过资格，未跑留出。开发基底为已发行的 v0.11.0 / `c9ff07d050c85ec198fb1137145f167d9976c24b`，真实 DA 各臂另外冻结其实际开发源码，不能称为未修改发行版基线。用户已授权完成 DA-1～DA-5 及约定真实测试，禁止全量 pytest 与 L2–L4；本合同不授予自动采用或另一次发行权限。

## 1. 执行组织与事实源

外层仍是一个 ProductTask、一个执行节点、原 Artifact、Verification、人工 Approval。single 与 adaptive 使用同一写入主 Agent；adaptive 仅额外获得宿主绑定的调查委派工具，可以零委派。主 Agent 根据问题临时组织任务，不要求预设 Planner/Reviewer 团队。

委派是原 Supervisor.create 与 Inbox/Delivery 的 NEW_TURN 组合，不建立队列、任务表、第二调度器或第二预算余额。原 Directory 记录 owner；原 Workspace 记录源与版本；原 Budget 记录预留/消费；原 Session/Effect 记录输入、工具和回答。Product 只保留原任务治理和跨域引用。临时工具绑定和活动 task 是进程内资源所有权，不是持久业务状态。

首版只允许主 Agent 派只读调查，子 Agent 不再委派、不向兄弟发送消息、不获得审批工具。子任务工作区来自该任务原始 base revision 的独立只读 checkout，不能读主 Agent 后续脏改动。没有父历史继承；父方在工作指令中明确提供的简要背景是未经验证的说明，不能增加来源权限。能力配置槽是宿主模板，实际调查职责由每次 goal 决定。

## 2. 工作与交接

模型声明调查目标、预期交付和简要背景；宿主绑定调用 Session、父 Agent、源码版本、工作区和额度。模型不能传 owner、实际目录、Provider、权限或预算。工作消息带格式、源版本及输入摘要，通过原 Inbox 接受；同一 Tool call 使用原稳定创建/消息身份，追问新增 NEW_TURN，不覆盖旧输入。

收集必须指定子 Agent 和 message identity，并重新核对当前调用者、Directory owner、Inbox 输入与 Workspace 源版本。报告由原 AgentRunReport 和 Session 派生，分别标明：消息终态、Agent 声明、已观察来源、主方实际消费。completed 只证明该次消息执行结束，不能证明自然语言正确或批准 Memory。交接不强制父方全量重读所有原文；冻结请求记录证明实际接收范围。

主方结束后，原执行 owner 必须先 dispose 并等待整棵子树收敛，再 capture Artifact 和进入验证。未完成调查被取消，其原失败/取消记录和成本保留。重复取消等待同一个 owned cleanup；部分 create/send 失败按原 Directory/Inbox/Budget 对账，不能把“没收到返回”当作“没发生”。不扩张崩溃恢复：只有原干净 Approval 屏障可恢复。

## 3. 预算口径

Budget 的 max_children 是直接子预留计数，max_depth 是可继续委派的剩余深度；process slot 对祖先计数，不把治理根自己计作自己的子槽。任务根→主 Agent→调查子 Agent 共两层，调查深度为 0。根的并发槽需要容纳主 Agent 与调查；主 Agent 的并发槽只约束调查。

根任务总额只授予一次。主 Agent 的账户包络包含自身和全部调查预留，调查通过原 reserve-before-create 扣减；关闭后仍由原账本结算，不把父预留与子消费重复相加。累计创建数与活动并发数分开。

为主方保留的整合 Token 在原 Budget.reserve_child 的准入线性化点检查，而不是工具先读余额后凭本地数值猜测。新增准入条件必须写入原预算事件以便重建验证；普通没有保留要求的原调用需明确声明零保留。不能使用 metadata 或工具提示发放预算。未知 usage 不作为零，也不能获得优化晋级。

主方显式单 Turn 墙钟上限与累计账户墙钟分开：原 usage hold 只预留两者较小值，使子预算可在主方运行期间分配。主方 single/adaptive 使用相同的这项限制；子方也使用明确上限。Budget schema 2 记录保留 Token 条件。DA-5 最终 Product 协议/事件/配置 3 绑定 coder/investigator 和每次对话限额，Product Context format 8 删除旧 routed 语义；旧 Product 1/2、Context 7 和旧 Budget 1 明确拒绝。已经启动的真实实验用冻结开发 Product 2 源码检查，不拿当前协议静默解析或改写。

## 4. 比较合同

原 EvaluationRunner / ProductTaskEvaluator 新增正式 execution_strategy 比较：同一源码、材料、模型、任务总额、工具基础能力与验证/评分，只允许 requested_mode 及必需委派装配不同。候选比较仍为 text_candidate，两臂模式必须相同。配对排除哪些维度由 comparison kind 决定；禁止移除 requested_mode 检查后随意拼报告。

comparison format 2 必填 kind 和 requested_modes；execution_strategy 的两臂模式必须显式给出，允许同模式 A/A。execution.first_arm 明确 baseline 或 candidate，独立 worker 的回执按 variant_id 核对，允许顺序交叉而不调换身份。源码候选不能进入执行策略比较。Product dataset format 2 的各 case 独立绑定 VerificationPlan；显式 null material_seeds 表示原冻结代码材料，没有随机种子。语义 Product 使用 product-durable-semantic-v1、精确 case rubric 与 requires_review=true，继续原 review/assess；隐藏验证与 rubric 不进入任务工作区。

从任务根与 Directory 重建整树 Session，包含主 Agent、所有子 Agent、失败/取消和未归因费用；按原 Session/Attempt 去重。交接诊断区分子读到、报告可用、父收到，不把委派数量当成功分。准备、分析和裁判费用另列。

Product 的语义 review/assess 接入原按 task_type 分派入口；原 durable gate 继续独立决定权限、验证、交付和收敛。声明需要语义审阅的题，在未审时保留 pending；语义裁判不能覆盖硬失败。无需语义审阅的确定性题不强制增加模型调用。

## 5. 实验规模与控制值

材料最终采用原仓库合同的缩减重建，附原始来源摘要，不冒称真实生产缺陷或原始生产目录。开发 12 题（三组各四题）、留出 6 题（每组两题、不同问题组），自然需求中不提示创建 Agent 或拆分角色。公开工作区不含参考补丁、评分期望或隐藏断言。18 份错误材料及参考修复已分别在固定 Docker 镜像验证，错误必须实际到达合同断言。源码、评分与额度冻结后才运行真实请求。

阶段调用上限仍为 A/A 8、机制 12、开发对照 48；有候选才追加 24，资格成立才留出 24，最多 116 个 task trial。分析及裁判另记。每个比较批次、题库/源码/模型/沙箱、任务与控制调用限额都在实际执行前冻结；不补跑直到成绩好看。

真实连接沿用用户已授权的现有 OpenAI-compatible 配置，由程序私下加载；实例明确选 qwen-plus、直连、不自动重试。评估 Shell/验证使用现有显式 Docker 配置的固定 Python 镜像与 network=none。具体 Token/步骤/并发值随材料预算清单一起冻结，不把本机路径或该模型写入通用默认。

晋级预设：硬门禁通过，质量无确认退步；至少一个组在两次重复中表现出质量改善，或质量相同且 wall elapsed 至少减少 10%；整树 task tokens 最多 single 的 1.50 倍。此为本次显式实验合同，不是生产默认。模型小样只作描述；未证明收益时保留默认 single，adaptive 显式可选，不自动采用候选。

## 6. 退出与阶段门禁

DA-1 验证真实并行、身份/范围拒绝、重复操作、保留额度、部分失败和重复取消。DA-2 验证 Product/TUI 原入口及捕获前子树收敛，并完成 20 个真实 trial。DA-3 补齐策略比较、Product 审阅和整树证据，执行 48 个开发 trial。DA-4 只开放批准的委派说明文本，通过原 AO-3 后台及有限原 AO 验证。DA-5 删除旧固定 multi/auto Router 及重叠配置，统一 single/adaptive，旧协议明确拒绝而不迁移数据。

DA-4 的批准文本位置仅 supervision/delegation.py 的 DELEGATE_GUIDANCE、FOLLOWUP_GUIDANCE、COLLECT_GUIDANCE、STOP_GUIDANCE。后台 Product 模板固定 adaptive/adaptive、显式 network=none 沙箱和无额外插件授权。真实任务的结构信号沿原 Product Reader 绑定原确认 Chat、工作区与相同持久账本，只有任务收尾后才接收，同一任务后续审批不重新触发。取消/拒绝并非语义失败，观察只作为未验证线索，不能成为 gold 或覆盖评分合同。

所有阶段只跑 owner 定向与相邻回归、编译/收集/Ruff/文档检查。已验证当前范围无 P0/P1 后停止扩张反例，进入下阶段；真实网络失败、暂定语义评分与确定源码缺陷分开记录。不会因为指标漂亮跳过用户采用权。
