# WC：受控可写协作执行合同

WC-2～WC-4 原限定验收已通过，记录 069 的单次新协议真实验收也完整通过：20 次调用、172050 exact tokens，预算与工作区收敛。固定检查首次通过，因此未验证失败后消费新反馈并返工的效果；稳定性与收益仍未证明。Promotion/验证协议 3，Session 15 / Context 13；旧失败保留、冲突项未运行，项目未提交发版。

2026-09-13 当前状态：single/multi、默认 single，WC-1D/E 已完成。WC-1F 失败保留；后续 WC-1G 同题完整确认已通过工程链与固定功能检查，主方自主补测及报告引用仍不充分。最新证据见[记录 051](../deal/051-full-collaboration-confirmation.md)。WC-2 已接入显式写助手与原 Capture，阶段定向门禁通过；WC-3 整合与对账已接入，定向门禁通过；WC-4 前六次真实失败保留；第七次记录 064 的冻结任务完整通过，当前真实调用已停止，收益与普遍可靠性未测量。

新任务从[交接入口](TRACEHARNESS_WC2_WC4_HANDOFF.md)恢复。当前文档更新不授权启动实现或真实实验，不覆盖历史失败。

## 1. 目标与停止边界

目标调整为：用户确认 multi 后，主方必须提交非空的主/子分工计划，明确目标、范围、排除项、交付与报告用途；宿主经原 Supervisor 派发并收回一个只读助手的报告，主方据此实施和验证。不再让模型选择 local；single 保持独立执行和默认模式。后续再让助手在独立工作区交付不可变 Patch，主方核对整合，沿原 Verification / Approval / Promotion 完成可写任务。新 multi 不恢复旧固定角色团队，也不承诺并行或收益。

路线收敛到可扩展静态 DAG：定义在运行前冻结并绑定摘要，运行时不让模型增删节点/边。复用原 Workflow，不新建图引擎、调度器、流程数据库或 AgentLoop。条件分支、子流程模板与新的节点注册机制后续按需求单独设计；当前五类节点不代表已经支持任意条件 DSL 或插件节点。

2026-09-13 追加：按需并发已按 [ADR-0077](../adr/0077-on-demand-concurrent-child.md) 实现，正式合同见正式上下文 14.3.19。本节"不承诺并行"指的是不承诺收益与多助手；当前允许的只有"一主一子、主方显式选择并发派发并自己按需收报告"，串行仍是默认。收益未测量，真实模型未验收。

首版仅一层助手、每个主方同时最多一个可写助手。主方工作区只有主方 owner 能写；助手仅写自己的工作区。没有共享脏目录、孙 Agent、兄弟广播、自动合并冲突、自动批准或后台扩权。只读 investigator 保留原含义；禁止直接把现有 delegate_investigation 改成可写。

## 2. 当前代码可复用的基础

| 能力及 owner | 当前事实 | 本次缺口 |
|---|---|---|
| `api/workflow.py`、`workflow/*` | 静态 WorkflowDefinition 与 AgentTask/Map/Join/Verification/Approval；图摘要绑定 | 不需新引擎；模板库扩展另立小阶段 |
| `product/topology.py` | single/multi 均为 coder → verification → approval | 维持外层流程；协作在执行节点内 |
| `product/registry.py`、`resources.py`、`runtime.py` | 原任务绑定、预算/工作区装配，当前助手模板固定只读 | 新的显式可写能力装配和 profile 校验 |
| `supervision/delegation.py`、Supervisor/Inbox/Delivery | 身份、消息、收集、停止、额度协商、所属关系 | 可写工作与产物交接接入同一生命周期，不能复制一套 Supervisor |
| `workspaces/supervision.py` | 每 Agent 的 managed workspace、capture gate；dispose 与工作区 release 分离 | 子方完成后先捕获，再按原 owner 释放 |
| `artifacts/capture.py` | 仅捕获 completed 消息、attached writable workspace、静止 Inbox/Delivery/Session；Manifest/CAS 原文与身份 | 将子方真实产物身份交回主方，而非信任模型自报 Patch ID |
| `promotion/service.py` | 外部目标的验证、审批、ref 更新 | 不能为内部整合伪造批准或复用外部推广绕过人工关卡 |
| EvaluationRunner / Product evaluator | 原任务验证、整树成本、报告证据 | 增加子 Patch → 主方整合 → 最终产物的证据检查，后续接策略对照 |

源码读取已确认：PatchCaptureService 要求最后一条消息完成、无 open claim/Turn/Step，并核对 workspace/agent/session/base_revision。因此“助手说完成”不直接等于“可以捕获”。停止 Activation 也不等于工作区已经释放。

## 3. 首版协作合同

### 3.1 创建与输入

通过宿主预先批准的能力配置允许可写委派；模型只能提出子任务和选择被授予的能力，不能指定任意宿主路径、密钥、工具或扩大预算。绑定 task、direct parent、child、Session、message、source identity、精确 base revision、预算与工具装配摘要。

首版子方基于任务确认时的同一冻结 revision，不复制主方完整历史或尚未提交的修改。目标、必要简报与显式证据引用进入子方自己的 Session。依赖主方新修改的任务暂不委派；不猜测应该读取哪个版本。

可写助手首版只授予工作区读取/搜索与 apply_patch，不授予任意 shell、网络、安装、Git 推广或递归委派。验证由主方与原固定 Verifier 在既有沙箱中执行；后续若需要子方跑测试，再设计专门权限组合。独立工作区是写入隔离边界；任务声明的交付路径范围在产物接收时核对，不能把它描述成已经实现的文件级沙箱。

### 3.2 完成与交付

原 Delivery/Session 记录决定消息是否完成。宿主通过原 CaptureGate 和 PatchCaptureService 捕获子方产物，交接只传 artifact_id、源版本、消息身份及报告引用，正文回到原 Artifact Reader/CAS 读取。拒绝失败/取消消息、错误 owner、过期消息或版本错配。

报告正文是模型主张；产物存在只证明修改存在。完成、捕获、主方读取、整合、验证通过分别保留证据，不能合并成一个“成功”。重复 collect 返回同一身份的观察，不重复 capture 或应用修改。

### 3.3 主方整合

补一个由主方调用的有界产物整合能力，复用原 Tool Effect、Workspace 写入 owner 和 Artifact Reader；不向助手开放。必须核对直接所属关系、同一 Product/source/base、完成消息、CAS 完整性与主方当前文件状态。主方先读取/检查 diff，再显式请求整合；不因收到报告自动写入。

这是主方受管工作区内的编辑动作，不更新外部目标 ref，也不创建审批事实。最终仍由主方捕获汇总 Patch，经过原验证、人工审批和推广。

整合只接受文件前像与预期一致的改动；同一文件已被主方修改或目标漂移时，首版明确报告冲突并保持主方已有修改，不静默覆盖、不自动三方合并。主方可依据证据自行重新实现修改；该行为需按普通工具操作记录，不能冒称子 Patch 已成功应用。

WC-2 编码前必须冻结整合 operation identity、源 artifact、目标 workspace 及前/后像回执。实现位置归 Workspace 的受控编辑服务，工具仅适配；若现有服务没有原子多文件发布，采用预检与可证明的回滚/部分结果对账，不能声称有全目录事务。重复操作、取消与持久回执结果不明时不得自动重试；从原 Effect/产物/实际文件核对，无法证明则明确失败。禁止增加可变 applied_patches 缓存充当事实源。

### 3.4 生命周期与协议

同一助手 capture 与 followup 必须通过原生命周期边界串行化；首版完成交付后不再复用该可写助手接第二个修改任务。先交付并捕获、再释放工作区；主方结束前整树工作收敛。失败、取消、发送失败及捕获失败均需完成原 owner 的清理；重复取消不能让工作逃逸。

只读 investigator 的已有语义不变；可写能力不得藏在 investigation 布尔字段或只读工具的可选参数里。WC-2 选择最小明确类型与接口，列出受影响的持久 schema；若协议需切换，按 pre-1.0 规则拒绝旧格式，不做猜测迁移、双字段兼容或用户数据自动删除。ADR-0069 当前继续描述只读实现；可写实施时新增 ADR 说明扩展，不改写历史。

## 4. 分阶段执行与验收

### 4.1 WC-1 的已验证事实和修订原因

WC-1A 的历史合同见[结构化只读合同](TRACEHARNESS_WC1A_STRUCTURED_READONLY_CONTRACT.md)。WC-1B 已接入最多两步侦察、来源冻结的独立决定、宿主派发和指定报告回收，见 [ADR-0070](../adr/0070-source-bound-step-views-and-structured-collaboration.md)。244 项定向检查通过，但该版允许 local。

WC-1C 一次真实任务中，主方提交合法 local，任务完成而没有助手，详见[记录 042](../deal/042-structured-real-acceptance.md)。不把程序派发说成从未实现：DA-7/8 与 WC-1B 都已做过。新差异是 **multi 的必经分工合同、主/子职责边界和失败时不能退回单干**，不继续追逐自主委派概率。

具体新规则以 [WC-1D～WC-1F 计划](TRACEHARNESS_WC1D_MULTI_EXECUTION_PLAN.md)为准：单一计划工具、一个只读助手、先收报告再实施；角色目标随任务生成，不使用动态 DAG。语义去重由说明与评估检查，结构/权限/身份/幂等/资源收敛由原程序 owner 执行。主方核验证据不属于重复分工。

历史合同、Prompt 和试验结果保留；adaptive/local 已被 single/multi 替换。自主准备按 [ADR-0074](../adr/0074-autonomous-collaboration-preparation.md) 取代固定侦察/独占规划，不加双模式别名或旧数据迁移。

### 4.2 总体阶段

| 阶段 | 工作 | 完成条件 |
|---|---|---|
| WC-0 / WC-1A | 历史整理与原合同 | 已完成，历史记录保留 |
| WC-1B / WC-1C | 原结构化决定实现与一次实测 | 工程检查通过；真实未触发助手，协作未通过 |
| WC-1D：multi 合同 | 审阅新计划，实施前冻结字段/版本/提示/停止语义 ADR | 已完成，现行为为 single/multi 必经分工 |
| WC-1E：multi 最小实现 | 模式入口一致、必经分工、工作信封、报告门禁和相关评估绑定 | 定向、相邻与反向验证通过，single 隔离，两份上下文同步 |
| WC-1F：真实只读闭环 | 新合同下一题一次真实主方与助手 | 有效分工、真实报告送达并使用、Product completed、资源收敛和独立重放 |
| WC-2：可写能力与产物交接 | 原装配、Workspace、Capture 与报告 | 身份/隔离/失败/取消/重复和 capture 生命周期验证，尚不自动整合 |
| WC-3：主方整合 | 原产物读取与受控编辑、回执、冲突处理 | 部分失败、重复、取消、错误 owner 和目标漂移可复现；不丢主方修改 |
| WC-4：真实可写交付 | 一个明确编码分工任务，两方真实模型 | 子 Patch 有用、主方整合、固定检查与完整 Product/清理/重放通过 |

原 WC-1F 失败后停止扩权；WC-1G 后续完整确认已通过工程链与宿主固定检查，下一阶段可在用户授权后推进，继续保留自主补测不足的限制和固定 Verifier。WC-2/3 按原 Finding 准入规则做限定复审，不借本轮扩展威胁模型、增加调度器或启动全量。完成闭环之后才另行授权 single/multi 同题比较，复用原 Evaluation；强制协作不自动代表优于 single。

## 5. 真实测试和费用边界

历史 WC-1C/F/G 已结束，原调用与失败记录不改写。下一次 WC-4 须获得覆盖该轮的真实测试授权，整树最多 32 次、600 秒、连接 60 秒、零重试。新计划不复用旧验收剩余额度，不再沿用“旧两题合计 48 次”覆盖追加实验；所有运行分别冻结、分别计费，预算不改生产默认值。

WC-1F 题目只写业务要求，由确认的 multi 模式要求分工；不能往通用提示或生产分支塞示例名称。WC-4 选一个可用冻结测试核对的非空代码修改任务，主方也承担必要整合/验证；避免只改注释或复制已给答案的空验收。先以空交付失败、合格参考通过做材料预检；语义核读与硬指标分别报告，不用 LLM 自述代替证据。

调用前只核对当前已授权连接是否仍可用，不打印秘密；连接失效停止，不反复排查消耗额度。只复用用户启动的 Docker 和明确固定镜像，不启动/修复环境、不拉镜像。任一 Provider 失败、封顶或取消，保留原结果并停止该轮，不提高上限追跑。最后逐项报告实际调用、精确/未知 usage、证据、失败和停止原因。

源码阶段运行 compileall、owner 定向与相邻回归、collect-only、修改范围 Ruff、协议拒绝及文档检查；关键保护做反向验证。禁止全量 pytest、L2–L4、Wheel、安装、提交/推送/tag/release，除非用户另行授权。

## 6. 当前遗留与工作区分组

本轮开始 git status --short 有 162 条记录（包含目录级未跟踪记录，不是 162 个文件或本轮改动数）。只做逻辑分组，不暂存、回滚、搬移或删除。

| 分组 | 代表范围 | 当前处理 |
|---|---|---|
| DA 协作基础与预算 | Product、Supervisor、Budget、Workflow、TUI 与相邻测试 | 已有累积工作，独立于本轮合同；后续提交前核对完整功能边界 |
| 有界读取 | read_file/search_text/file_page 与定向测试 | 保留已完成结果，不为可写计划重做 |
| Evaluation / AO 接口 | evaluator、variants、product feedback、基准材料 | 复用；尚未证明多 Agent 收益，语义评分保留待审 |
| DA-6～DA-14 研究 | live 驱动、计划、记录与 validation-data | 保留失败和撤回证据，不把历史候选重新装入生产 |
| 其他未跟踪文档 | 学习笔记、claude-recmd 等 | 所属与交付必要性未逐件审定，不擅自并入提交 |

明确遗留：主方自主功能补测不足、部分报告引用不完整、语义裁判未校准可靠、多 Agent 收益未证明、工作区未提交发行。自然委派 0 作为历史观察保留；新的 multi 不再以自主选择委派为目标。后续可写机制/证据进入 WC-2～WC-4；收益对照需后续授权。静态 DAG 模板库排在 WC 之后单独设计，不扩大本轮范围。

## 7. 当前状态与证据

当前 single/multi，只读助手完整交接已在记录 051 验证。历史 [WC-1B](../deal/041-structured-readonly-collaboration.md)、[WC-1C](../deal/042-structured-real-acceptance.md) 的 adaptive/local 和零助手观察仅用于回顾，不是现行合同。

后续按 WC-2 捕获产物、WC-3 主方整合、WC-4 真实交付推进，逐阶段满足门禁。当前文档整理没有源码变更、Docker/模型调用、提交或发行；原调度、产物与权限 owner 继续复用。
