# ADR-0046：共享 Context 治理与冻结检索评估

- 状态：Accepted（v0.9-F5 开发范围；发布门禁另计）
- 日期：2026-09-07
- 前置：ADR-0043、ADR-0044、ADR-0045；v0.9 计划 §12。

## 决策

Line 与 Textual 调用同一 `chat/governance.py::ChatGovernance`。每次查看都从既有
Session、Project、Memory、Skill owner fresh read，窗口和确认 Future 只是临时交互状态。
`/context [STEP_ID]` 展示实际冻结 Context 与 dispatch 状态；`/skills` 明确区分当前目录、
持久选择、eligible 与最近一次实际注入。History 目录和分页继续用原有有界 reader、cursor、
source digest、cutoff、原始大小及 freshness，不开放跨 Session 原文搜索。

Memory 提议、approve、supersede、revoke 与 Project/Skill 选择操作先展示精确内容，再要求
`CONFIRM`。确认前保存 head、引用和 digest，确认后交回原 writer 做 CAS；取消不写入决定，
等待期间事实变化会拒绝旧确认。模型不能通过这些命令批准 Memory。已接受操作的失败或取消仍由
原 owned task、Lease、事务和 cleanup 收敛。

插件 enable/reload 先经现有 `PluginManager._load` 导入已显式选择的可信对象，显示完整 Manifest
与 provides，再进入 setup。导入本身会执行可信代码，provides 是声明而非沙箱或完整权限保证。
review 返回绑定同一对象及 manifest 摘要的 Discovery；正式激活重新检查摘要并走原
Activation/Generation/Lease/Drain 主线，不新增 loader、registry 或激活生命周期。

`traceh chat --context-config FILE` 读取 exact-key format-1 JSON，所有 Context/Skill/Memory
限额和 Git source 映射显式给出。Context 复用唯一 policy parser；恢复命令保留该文件路径。
与 Product 一起装配时，source 路径、managed root、resolver 实例、Store 和 Session owner 必须一致。
无此配置不隐式启用 Memory 或检索策略；semantic/reranker 保持 null。

## 唯一评估主线

根 `benchmark.json` 一次切到 protocol 2，必须有 `retrieval`（null 或 file/sha256）；拒绝
旧根 schema 1。嵌套 Verification protocol 仍是 1；Session 4、Context 3、SQLite 2 不变。
corpus format 1 冻结 policy、项目/Memory 限额、事实状态、插件目录与选择、人工 judgments、K、
类别阈值及资源上限。文件在 manifest root 内且不在任何 initial tree；执行前后重验 SHA-256。

`run_attempt` 是唯一 seeding owner：真实 attempt source → SQLite/Runtime → requester Session
及宿主项目绑定 → 生产 Memory declare/approve/supersede/revoke、Plugin 激活/退役/选择和索引重建
→ Product host → 同一 requester Session 的真实请求。完整 corpus/judgments 不复制到工作区，
不作为 evaluator 指令进入模型；事实通过正常的 Context 检索与预算进入请求。

Product 已有 `ProductProjectBinding.bind_agent` 在 attach 后证明 requester/task/Agent/Workspace
身份；显式 Memory 配置下，它在初次绑定及后续 send 的原有宿主边界准备该 Session 的派生索引。
原因是 Memory corpus identity 包含 Session 绑定及来源 head，不能复用 requester 的索引键。
这是宿主准备，不是 query-time 自动修复。索引失败/取消沿原 Supervisor/Workspace cleanup 返回。
角色 Runtime 使用相同 Context 与 Memory 配置，仍按固定 Profile 授予 Tool；Skill 选择不自动继承。

报告只读实际 `context/input` 与 `request/snapshot`，每个 Session/Step 计一次；Provider retry
不增加样本。候选排序与实际注入分别展示；tier relevance 以实际注入为准，同 kind/id 去重，
后续合格 tier 不改变首次身份排名。Recall/MRR/precision@K 与整个 Context 的 precision 分开；
zero-hit 表示未注入任何 Skill/Memory 身份。所有 dispatch 的 Step（含 unjudged 角色）都检查
退役/失效/跨项目隔离，违规必须为零。缺失判断保留 unproven，未 dispatch 保留 unavailable，
无阈值只报告测量；检索质量与 ProductTask 成功是不同字段。

报告保留每 attempt/role/query 的唯一 Step 观测、描述性汇总、冻结输入/evaluator 摘要、seed
事件引用、项目归属和目录摘要。成本包含 canonical Context 字节、完整 Context 准备时长、
索引 manifest 的逻辑 item_count/item_bytes、索引准备及重建时长、seed 总时长及 Python/SQLite/
Unicode/OS/架构版本。没有单独计量的纯排序耗时或物理 SQLite 索引大小明确不作推断。

## 冻结基线与边界

`benchmarks/retrieval_v1` 在候选结果前冻结 11 条中文/英文/代码查询，覆盖九种类别；需要显式
安装 `examples/plugins/traceh-reference-skills` 的两个可信 Entry Point。示例只贡献 typed Skill，
不贡献 Tool/Prompt。生产缺插件或目录摘要不符时拒绝，测试仅用源码 Entry Point metadata 替代安装。
semantic 类别的词法基线最低分显式为 0，记录其局限，不声称语义收益；其他冻结阈值不随结果调整。
当前 corpus 支持随插件加载的内联 Skill section，不配置外部资源根。

不新增 Runner、模型自评、插件自动安装、Widget 事实缓存、持久 pending 或兼容 reader。
不因 F5 开发自动运行全量、L2–L4、Wheel、真实 Provider 或发布操作。
Release Stop C 独立审查与最终发布门禁保留为后续检查点。
