# ADR-0044: 宿主持有的项目 Scope 与 Memory authority

- 状态：F0-A 设计已接受；ProjectScope/Memory 服务与流尚未实现。
- 日期：2026-09-07。
- 范围：v0.9 的跨 Session 项目关联、事实槽与审批；实现属于 F3 及其宿主接线阶段。
- 依赖：[`ADR-0043`](0043-step-scoped-context-input-and-retrieval.md) 的 Context 输入边界。
- 规范字段与验收清单：[`F0 设计合同`](../plan/TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md)。

## 背景与当前接缝

[`SessionService.create_session()`](../../src/traceh/session/service.py) 当前记录解析后的 workspace
目录，没有长期项目身份。[`WorkspaceService`](../../src/traceh/workspaces/service.py) 的 identity
绑定一次 provision 操作及请求；[`LocalGitWorkspaceProvider`](../../src/traceh/workspaces/local_git.py)
的 repository fingerprint 也是本机 common Git dir 路径的指纹，不是可跨移动识别的永久项目 UUID。

因此换一次 worktree 就新建一份 Memory 会丢失长期语义；按路径、source 名称或模型提供的 project id
共享 Memory 又无法证明归属。Memory 的“冲突”也不能只靠相似度定义：检索器没有批准项目事实的权限。

## 决定

### 1. 独立的项目关联事实，复用同一 EventStore

新增宿主 `ProjectScopeService` 与纯读 projector。全 Store 唯一 `projects:catalog` stream 按 CAS
追加 `project/created`、`project/source-bound`、`project/session-bound` 三种事实。这里只记录
项目关联，不管理 Agent 或 worktree 的创建、释放、迁移与恢复。

项目 identity 是宿主显式创建的 opaque `project_id`，只在当前 Store 中解释；服务实例还必须持有
同一 Store 的 reader/writer 能力。字段字符串相等不证明另一个 Store、SessionService 或 host 的所有权。
模型输入不能直接创建／选择 scope；CLI/Driver 使用显式宿主配置或该 Session 已有唯一合法绑定。

`source-bound` 记录宿主解析的 source identity 和可验证 repository evidence；这些值用来核对本次
装配，不自动推断跨移动、clone 或 source mapping 变化后的项目身份。v0.9 每个 project 只绑定一个
source，且 source 的规范身份在同 Store 只属于一个 project。缺失、歧义、不同绑定明确拒绝；不提供
路径模糊匹配、全局默认项目、自动 merge 或 rebind。

### 2. Session 绑定的线性化点只有一个

`session-bound` 的成功 append 是 Session 获得项目 Memory 资格的线性化点，包含同 Store 的
Session 创建事件、project/source 证据和宿主继承依据。全局 catalog CAS 保证并发请求不能把一个
Session 分配到两个项目。相同 operation identity/exact payload 重复提交返回原 receipt；不同
operation 即使目标相同也必须读回既有绑定，不能写成第二份所有权。

普通 requester Session 在第一条项目上下文请求前由宿主显式绑定；宿主 source resolver 必须证明
`session/created.workspace` 实际属于该绑定 source。后续 Session 也须完成这条核对，不能仅选择
project_id；目录同名本身不充分。managed child 则由现有 Product/Workflow/
Supervisor 装配 owner，在 Workspace、Directory、Session 附着事实已建立后、首次执行前，调用同一
scope service 追加绑定。继承依据必须 fresh 追溯 requester/task/owner 与工作区 source，不能把
`AgentSpec.metadata` 当批准书。

这些跨 Stream 步骤不声称原子：关联未完成时禁止 Memory 检索和首次项目模型执行；失败／取消由现有
创建与 dispose owner 收敛，留下的无效或已释放对象事实不删除。已提交绑定不撤回；取消重试先收敛
worker、精确对账 True/False/unknown，unknown 不另造绑定。释放 worktree 不删除项目 Memory。

F0-B/C 的当前 Session History 可保持无 project binding，并如实在 Context 标注 Session-only scope；
这一最小原型不因此获得 Memory 权限。

### 3. 一条项目 Memory stream，宿主批准 exact proposal

每个 project 在同一 Store 使用唯一 `memory:<project_id>` stream，Memory service/projector 是该
领域唯一 writer/reader。索引、Context、UI 与模型只取得需要的只读能力；proposal Tool 可以提交
有界候选，但没有 approve、supersede、revoke 方法。

宿主维护的 `fact_slot` 表示一个可被替代的项目事实位置。模型可提出正文与来源，不能凭自报 slot
抢占已有事实。宿主先选择或显式创建 slot、核对来源、scope 与 exact proposal digest，再批准；槽名
来自显式业务输入，不预置某个 Demo、模型或项目的专用槽。

状态与线性化点：

| 当前状态 | 宿主动作 | 结果与必要绑定 |
|---|---|---|
| 未存在 | propose | 追加 candidate，记录准确正文／source／digest；不产生 active 或抢占 slot |
| proposed，slot 无 active | approve | 核对 proposal/head，CAS 追加 activation；该 slot 恰有一个 active |
| proposed，slot 有 active | supersede | 在一个事件内消费新 proposal 并替换 exact predecessor id/digest；不存在双 active 中间态 |
| active | revoke | 核对 exact active id/digest/head，CAS 追加撤销；该 slot 无 active |
| superseded/revoked | approve/revoke | 拒绝复活或重复变更；exact operation 重放只返回原结果 |

每次控制动作保存宿主 actor、operation identity、expected stream head 与精确批准对象摘要。
并发 approve/supersede/revoke 只允许一个 expected-head CAS 成功；败者重新读取并报告冲突，不能
偷偷改成覆盖最新 active。revoke 后恢复结论必须提出新 proposal 再批准，不复活旧记录。

### 4. 来源、冲突与时效各有边界

第一版 source 是三种严格 variant：同项目已绑定 Session 的允许 Surface evidence 引用、通过同项目
requester 关系核对的 ProductTask 原流事实引用，或宿主人工声明。前两者记录 exact event digest 和
observed boundary；后者明确显示为人工声明，
不能伪装执行证明。来源可审计不代表一般模型可跨 Session 展开原始对话；Memory 只披露批准正文和
有限 provenance，跨 Session 原文访问仍不在 v0.9 默认范围。

同 slot 的重复 active 是确定性冲突，由 Memory projector/CAS 防止。跨 slot 的语义矛盾只能提示
宿主审核，不能称 FTS、LLM 或字符串检查已经证明“全项目无矛盾”。当前 Product/Workflow/Workspace
状态仍从原 owner fresh 读，Memory 不能撤销任务 requirement、授工具权限或改写验证与批准。

Memory 是有界短事实：目录按宿主固定规则派生 metadata，摘要／正文保留完整批准内容，不截断限定句；
首版没有独立关联资源，不能把 Memory chunk 请求解释成跨 Session 来源展开。完整批准正文摘要与实际
披露 block 摘要分别计量，只有展示完整正文时二者相等。

Context service 从 fresh replay 的 approved active 集取得有界参考，再核对 scope/head/digest；
最终复核发现变化就拒绝本次候选或显式记 unavailable，不替换成 latest 正文。该次成功复核是这个
来源的选择观察边界，随后的变化影响后续 fresh 选择；Context append 不声称所有来源在同一瞬间最新，
也不新增跨流原子事务。已经冻结的历史
Context 永远使用当时 bytes；后来的 revoke 不篡改过去，也不能让新 Step 自动沿用旧批准。

### 5. 存储、生命周期与权限保持原边界

projects 与 Memory 都是已有 EventStore 中的新 append-only Stream，不是额外 SQLite 事实表或
Workspace 状态机。索引完全派生，删除重建不能批准、撤销或更改 Memory。两个服务复用已有 CAS、
owned append、三态对账和关闭规则；不为它们添加另一 Runner、Agent activation 或 recovery 流程。

## 被否决的方案与代价

- 用 worktree identity 作 project：一项长期事实随临时执行目录被拆散。
- 用路径、source 名称或模型自报 scope 自动共享：混淆名称相同与宿主授权。
- 将绑定复制进 Session metadata、Workspace、UI 缓存同时维护：形成多个冲突事实源。
- 每个 Memory 独立流且不共享 slot 线性化点：并发批准无法保证同槽唯一 active。
- 检索相似度决定 supersede：将事实审批权交给 derived 算法。

代价是首次建立项目和换 Session 时需要明确宿主关联；跨路径移动与 rebind 暂不支持。全 Store 项目
catalog 与每项目 Memory stream 采用现有有界串行写入，先服务当前单机规模。F3 必须通过跨项目、
跨 owner、并发 CAS、失败取消与真实 requester/child 继承路径验收，F0-A 不声称已实现这些保证。
