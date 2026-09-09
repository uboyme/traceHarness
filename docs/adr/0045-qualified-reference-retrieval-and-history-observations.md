# ADR-0045：共享引用检索与有来源的历史版本观察

- 状态：Accepted（v0.9-F4）
- 日期：2026-09-07
- 接续：[ADR-0043](0043-step-scoped-context-input-and-retrieval.md)、[ADR-0044](0044-host-owned-project-scope-and-memory-authority.md)

## 背景

F2 已有 Skill 检索，F3 已有 Project/Memory authority。F4 需要让 Memory 进入同一请求主线，
并让历史工具结果的版本标记来自实际观察。把三类来源混成通用文本库会丢失资格和权限边界；
把 Workspace 的 base_revision 当成当前版本则会把旧证据错误标成有效。

## 决定

1. 保留各来源 reader 与权威流。Skill 使用 leased catalog 与持久选择；Memory 使用原
   ProjectScopeService/MemoryService 的 active view；History 只读当前 Session 的 replacement 图。
2. `ReferenceRetrievalPolicy`、normalize/tokenize、eligible BM25、RRF、receipt 校验由同一 owner
   维护。Skill 与 Memory 各自形成已获资格的有界 corpus；不共享 df/avgdl，防止其他来源改变统计。
   两个 corpus 的有理数融合结果按同一内容身份排序，再共同接受 item/kind/total/max_blocks 限制。
   History 不排名，合法原文请求先于自动历史，再处理 Skill/Memory 显式请求和自动候选。
3. F0 合同 §5.2 的通用配置目标在此细化为唯一十二字段 Context 配置和两个同类型的十五字段来源策略。
   保留来源独立配额与限额，不引入没有生产实现的 query/lane 插件注册层。仅本 Turn 首条真实用户输入
   是查询来源；semantic/reranker 明确为 null，启用未选定实现明确拒绝，不加载或下载模型。
4. Memory 的 version 是 proposal_digest，引用 proposal 与 activation，正文是完整已批准事实。
   directory 只披露规范元数据；summary/section 使用完整正文，不生成另一份摘要；不支持 chunk 或
   跨 Session 原始来源展开。只读 Tool 仅返回一次性 receipt，生命周期复用既有 Step/Tool 事实。
5. `workspace_observations=True` 必须显式配置 Memory authority、Memory 检索、有界 History reader，
   resolver 必须提供只读 `project_observation(source_id, workspace)`。LocalGit 实现复核注册身份，
   在 `status` 前后读取 HEAD，仅干净且两次相同才返回 revision；其他情况保持 unknown。
   ToolRuntime 对工作区读组、串行写和 process 在执行前后观察，仅相同观察写入宿主 Tool envelope。
   Tool 正文不能自行提交此证明；取消复用原收敛与 Effect 对账。
6. History 的 freshness 从允许叶子中的真实 Tool envelope 与当前 Context 冻结观察派生：有可比较
   冲突为 stale；所有相关工具都有相同的已知干净版本为 matched；其余为 unknown。matched 只表示
   所观察 Git 版本一致，不证明测试、外部环境或工作区在返回后依然不变。
7. Session marker 切到 4、Context format 切到 3、policy 切到 `f4-context-policy-v1`、renderer 切到
   `context-json-v4`。SQLite schema 仍为 2，没有 DDL 变动、迁移、别名或双读。历史重建只重放冻结
   prefix/receipt/bytes，不查询当前 Git、当前 active 或当前索引。

## 代价与边界

每个来源以最后成功复核为观察点，没有跨事件流原子快照或跨进程文件锁。Git 观察增加有界本地 I/O；
未绑定的 Session 和缺失观察不能宣称匹配。没有新增 Store、Runner、Projector、权限、后台任务或
可变 pending；没有语义质量提升的承诺。F5 的治理界面、冻结质量评测与最终发布门禁仍是后续工作。
