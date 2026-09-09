# ADR-0057：语义摘要使用当前 Turn 的普通模型 Step

状态：已实现；分层压缩 D，2026-09-09。补充 ADR-0053/0055/0056，取代 ADR-0042 中“尚无合法模型摘要请求”的限制；不改写历史 ADR。

## 问题与决策

规则摘录不能可靠概括目标、约束和未决问题。直接在 `SessionSummarizer` 中调用 Provider 又会绕开当前请求许可、Budget、取消和精确重建合同。

因此增加一种冻结请求来源 `summary/input`，让摘要占用当前 Turn 的第一个普通 Step。使用同一 Composition Lease 捕获的模型、原 `LlmRuntime` admission、Budget reservation、`SessionService.start_model_attempt()`、重试和终结逻辑。摘要成功后进入第二个普通对话 Step，重新冻结当前参考资料并回答当前问题。没有第二个 Provider 调度器、Session、后台任务或可变历史事实源。

## 所有权与协议

- `CompactionService` 仍独占来源选择与 `surface/replace` 写入。先执行 C 的旧工具结果折叠，再按 E0 完整请求压力决定摘要。只选择闭合旧前缀，排除最近配置轮数和当前开放 Turn；不拆工具调用/结果，不压 Product 当前状态、Skill/Memory 授权资料或审批。
- `summary/input` format 1 冻结 Session/Turn/Step、观察 head、cut、来源 seq/digest、Composition revision、完整压缩/token 策略和提示词 digest。来源和配置由共享 reader 重算，只允许首 Step；同 Step 不得混用 Context 来源。
- `summary_model_request()` 是纯构造函数：固定系统说明加带来源序号和原角色的历史 JSON，工具列表为空，模型、温度、输出上限来自已冻结 Composition。历史是资料，不能作为当前指令执行。
- `request/snapshot` 增加严格判别形状：普通请求保留 `context_input_seq/digest`，摘要请求使用 `summary_input_seq/digest`，其余许可、请求指纹、来源截止点与派生 dispatch 合同相同。dispatch、重建、检查、计量和界面共用该规则；不能伪装成普通 Surface 请求。
- 完整输出记为 `summary/response`，不是聊天 `assistant/message`。普通 `model/attempt-end` 记录 usage，随后 replacement 的 `causation_id` 必须指向同 Session/Step 的真实成功摘要响应。源检查同时核对响应、来源、摘要正文和策略；模型输出本身不能批准 replacement。
- Session 10、Context 9、SQLite 2 和 Surface format 2 不变；增加的是明确的来源形状和 `method=semantic`。旧 reader 会拒绝新形状/method；不补写旧记录、迁移、删除或猜测格式。摘要提示词属于冻结协议身份，不能让旧摘要按另一份提示词通过重建。

## 内容、费用、失败和恢复

摘要是完整 JSON：`goal`、`constraints`、`verified_progress`、`reported_progress`、`decisions`、`open_questions`、`evidence[{source_seq,note}]`。只有用户采纳的决定才进入 decisions；助手陈述必须区分于工具实证，成功检查不能外推为整个任务完成。宿主校验精确字段、非空目标、列表形状、真实来源指针、完整 finish reason、无工具调用和字节上限；超长或截断直接拒绝，不能切半个 JSON 当成功摘要。

这只保证结构、来源、权限与调用证据，不能机械证明自然语言的全部语义正确。原事件保留，摘要是历史参考；遗漏细节通过当前 Session History 原文页查证，不提升为已批准 Memory。模型可能遗漏或解释错误，验收不能只检查有没有输出。

摘要和回答共用 Turn 的步数及费用；启用需至少 2 步，摘要响应也进入实际 usage/Budget 结算。每轮最多一条摘要调用链，传输重试仍使用同 Step、同冻结请求及原重试策略。Provider 耗尽、Budget 拒绝、取消按原 Turn 收敛；不裸调用、不自动换模型或无限重试。格式不合格则记 `surface/compaction-failed`，旧历史不被该摘要替换；若正常请求仍在硬上限内可继续，超限则拒绝。之前已经提交的 C 折叠仍是有效历史呈现。

摘要提交继续使用 Store CAS、owned append 收敛和提交后对账；发生并发时重验同一冻结来源，不能用旧模型输出总结新来源，也不重复调用模型。进程崩溃后，已持久化完整 `summary/response` 可支持关闭孤立 Attempt；缺 response 时为 unknown。恢复不补造 usage、不重新调用模型、不自动应用尚未提交的摘要；已有 replacement 保留。

显式 `TurnInput.history_requests` 保留首 Step 披露优先级：该轮跳过语义维护，直接满足原请求，仍受 token 硬上限限制。后续普通轮才可摘要。摘要 Step 不消耗这类首 Step 阅读许可。

## 配置和范围

默认仍为显式可选的规则摘录。`RuntimeConfig.semantic_summary=True` 或 CLI `--auto-compact-method semantic` 启用；同时必须开启自动压缩并填齐 E0 Token 预算。F2“自动压缩”页提供中文选择，配置 profile format 1 新增可选 `auto_compact_method`，共 26 项；缺失表示默认摘录。缺少依赖配置时显示固定可读提示，不透传异常或密钥。Ctrl+X 标明最近请求用途为“历史语义摘要”或“对话”。不修改用户的已有启动配置。

D 不实现 E 的轮内自适应压缩、逐类 token 资料裁剪、目标占用比例或 Provider 超窗恢复。摘要请求自身也必须满足 E0 硬上限；这里没有分批摘要兜底。

## 验证

见 [D 真实验证](../validation-semantic-summary.md)、[专项测试](../../tests/test_semantic_summary.py) 与 [真实案例入口](../../tests/live_semantic_summary/run.py)。验证涵盖真实成功/失败检查、遗漏原文读回、重启与换题；预算拒绝、错误响应、取消、CAS、重试、恢复和反向验证。只运行相关门禁，不运行全量、L2–L4，不提交或推送。
