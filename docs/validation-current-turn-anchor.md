# 本轮原始请求定位修复验证

日期：2026-09-08。范围：修复历史检索后换话题仍重复旧答案。实现决定见 [ADR-0051](adr/0051-current-turn-request-anchor.md)。以下活动名称、人员、口令和位置均为显式测试例子，不是系统默认值。未提交或推送。

## 根因和修复

原会话的新问题已经写入独立请求，HTTP 适配器没有缓存旧答案。参考目录最后作为 user 消息呈现，当前问题的定位不充分。授权的真实对照对“你好”和“你是谁”各发三种请求：原呈现均重复旧答案；保留参考包并明确引用当前问题均恢复正常；删除参考包并同步修改系统定位说明也恢复正常。选择保留后置参考的方案。

ContextInputService 从当前 Turn 第一条真实 user/message 派生 active_request 的原文与 EventRef；同 Turn 工具续步沿用，换 Turn 重新绑定。Session append 在原观察前缀下验证绑定，renderer 将原话和定位说明放在参考内容末尾。未改写 Surface，未新增事实源，Provider 不追加未冻结消息。引用配额与必需回显分别记账，完整模型消息字节可重算。

当前协议为 Session 10 / Context 9 / context-json-v9。旧 Session 1–9 明确拒绝且保持原账不变。用户已有数据、启动配置和密钥文件未改写；体验新版请使用新数据目录和新 Session。

## 最终定向验证

以下是不同测试组的实际结果，组间存在重叠，不应相加冒充独立用例数。

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 来源、预算、请求与 History | `python -m pytest tests/test_active_request.py tests/test_context_input.py tests/test_context_request_protocol.py tests/test_reference_read_actions.py tests/test_reference_retention.py tests/test_history_runtime.py --tb=short --durations=5` | 94 passed，47.22s |
| Surface、不变量、TUI 检查及相邻 Product | `python -m pytest tests/test_active_request.py tests/test_surface_and_invariants.py tests/test_tui_context_inspection.py tests/test_product_f3_e2e.py::test_completed_product_status_reaches_the_next_request_without_authority tests/test_product_f3_e2e.py::test_auto_product_mainline_converges_with_the_production_sqlite_store --tb=short --durations=5` | 63 passed，50.65s |
| 架构边界 | `python -m pytest tests/test_product_architecture.py tests/test_v07_d0_architecture.py tests/test_future_protocols.py --tb=short` | 23 passed，3.27s |
| 编译 | `python -m compileall -q src tests` | 通过 |
| 修改范围 Ruff | `python -m ruff check src/traceh/session/context_input.py src/traceh/session/protocol.py src/traceh/runtime/prompt.py tests/test_active_request.py tests/test_context_input.py tests/test_history_runtime.py tests/test_reference_read_actions.py tests/test_surface_and_invariants.py` | 通过 |
| 只收集用例 | `python -m pytest --collect-only -q` | 3401 项，未执行测试 |
| 差异检查 | `git diff --check` | 通过；Git 有现存换行转换提示 |

定向实现中曾有 8 项旧渲染解析/预算断言失败，以及另一组 15 项缺失真实用户输入的测试夹具失败。已更新解析参考 JSON 的位置、引用配额断言及真实输入夹具，上述最终相关组通过。另一组中的 Context Runtime、Memory、Skill、OpenAI Provider 相邻用例当时通过；不以整组 238 passed / 15 failed 的中间结果宣称完成。

关键反向验证：临时移除 active_request 来源绑定校验，运行 `test_recomputed_anchor_digest_cannot_substitute_another_fact` 两个参数，均在公开 append 路径出现 `DID NOT RAISE ContextInputError`，证实伪造内容或其他 Turn 的来源被接受。随后逐字节恢复源码，最终 94 项包含这两个正向拒绝用例并通过。

## 授权的真实模型旅程

通过现有配置加载凭据，仅在进程内使用；生产 Runtime、SQLite、openai-compatible/qwen-plus，模型回复未脚本化。工具策略只允许 History 读取。隔离工作区未产生文件。先输入两条临时事实，再压缩为不含这些细节的摘要，然后使用自然问句检索和换题。

最终显式测试政策 page_messages=20（测试设置，不是声明系统默认值），共 8 轮用户输入、9 次模型 attempt。逐轮证据见 [真实结果](validation-data/current-turn-anchor/normal-page-live.json)。

| 操作 | 实际结果 |
| --- | --- |
| 询问口令、接待人、胸牌位置 | 实际调用 request_history_page，读到第 0 页，正确回答潮汐-583、许宁、西门绿色箱子的第二层 |
| 你好 | 你好！ |
| 你是谁？ | 正常介绍当前编码代理身份 |
| 17 加 26，只答数字 | 43 |
| 关闭 Runtime 和 Store 后重新打开，早上好 | 早上好！ |
| 恢复后再问安排 | 正确回答口令和位置；本轮没有再次读取 History，Surface 中已有前次正确回答，不能算作第二次原文检索成功 |

所有 Context 的 active_request 均匹配本轮输入；完整呈现字节与预算一致；关闭前与恢复后的请求重放无差异，不变量检查通过。

## 保留的失败与能力边界

初次真实旅程在历史读取后遇到 provider-tls-eof，记录为网络失败。之后使用现有 CLI 的重试配置执行，未修改生产重试策略。

强制 page_messages=2 时，第一页只含前两项事实，下一页才有位置。模型没有继续调用工具，错误地认为位置未说明，见 [小页失败](validation-data/current-turn-anchor/tiny-page-live.json)。追加通用翻页提醒仍未解决，见 [无效提示实验](validation-data/current-turn-anchor/tiny-page-ineffective-hint.json)，该提示已撤回。重发修改前呈现的真实对照也漏读位置，见 [旧呈现对照](validation-data/current-turn-anchor/pagination-baseline.json)。该对照支持它是既有分页使用问题，不支持声称任何模型在所有场景都如此。

因此：本次真实验证证明换题定位在以上旅程中恢复正常，未证明模型总会自行翻完需要的历史页。小页实验的 completed 只代表调用流程结束，不能代表语义通过；保存的证据另标 semantic_result 为失败。确定性工具分页与保留测试通过，也不能替代真实模型分页语义验收。

## 文档与未运行门禁

正式版先更新，通俗版随后按正式版同步：第 1、4、7.4、15、16、17 节解释状态、owner、流程图、预算、验证与边界；第 6、7、13 节等现行协议描述同步版本，13.3 的引用预算描述同步新账本。同步 README、CHANGELOG、TUI 配置说明和外部逐行体验文档的启动提示。历史 ADR 不改写，新增 ADR-0051。文档检查通过：两版 0–20 共 21 个一级编号章节对应、7.4 对应，检查范围内相对链接存在，Mermaid/代码围栏闭合；新增流程图与实际派生、构建及重放方向一致。凭据模式检查未发现真实 Key，通用实现未包含测试活动、人员、问候或特定模型分支。

未运行全量测试、L2、L3、L4、Wheel 或包索引门禁。没有迁移用户旧 Session，也没有修改启动配置自动指向新数据。
