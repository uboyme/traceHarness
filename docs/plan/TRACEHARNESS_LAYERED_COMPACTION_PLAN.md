# TraceHarness 分层压缩实施合同

本计划对应 ADR-0053、ADR-0054、ADR-0055 、ADR-0056 与 ADR-0057；A/B/B+ 已完成，B+ 最终 16 回合真实验收见 [关键词查找专题](../validation-tool-output-search.md)，C 已完成，586 项定向检查和 14 个真实回合通过，见 [C 验证](../validation-tool-result-folding.md)；E0 已前置完成完整请求 token 估算与用量对照，见 [E0 验证](../validation-request-token-meter.md)。执行顺序为 A/B/B+/C → E0 → D → E；D 已完成同一模型 Step 的语义摘要及真实验证，见 [D 验证](../validation-semantic-summary.md)；E 已获目标模式授权；E1 轮内旧历史维护已通过定向与真实验证，见 [E1 验证](../validation-in-turn-compaction.md)；E2 已完成参考 token 准入及六组真实对照，见 [E2 验证](../validation-reference-token-budget.md)；E3 已完成超限体验及真实旅程，见 [E3 验证](../validation-context-acceptance.md)；语义检索复测也已完成，九组未达质量门槛而保持关闭，见 [复测报告](../validation-semantic-retest.md) 与 [执行合同](TRACEHARNESS_E_AND_SEMANTIC_EXECUTION.md)。
用户约束：真实工具与真实模型验收；只跑 owner/相邻定向测试，不跑全量和 L2。

| 阶段 | Owner 与交付 | 验收条件 |
|---|---|---|
| A：合同冻结 | 明确保护内容、来源、完整请求预算口径、各层触发与失败边界 | 不把 bytes 当 tokens；不把摘要当权限；不新增可变 messages 事实源 |
| B：保存/目录/读回 | 原 ToolRuntime/Effect/SessionService 保存完整结果，普通只读工具定位并分页 | 真实大输出、非零 exit、Unicode、重启、压缩后查证、隔离、失败/取消、恢复、反向验证及真实模型 |
| B+：关键词查找 | 复用 B 原文与来源校验，普通 PURE_READ 字面搜索返回有界片段和读回位置 | 真实模型先搜索再按需展开；重复/无命中、长行/Unicode、压缩重启、隔离/取消、反向验证 |
| C：旧结果折叠 | 原 Surface replacement/projector owner 扩展已结束旧结果的呈现规则 | 保留完整调用/结果结构；近期内容不动；折叠和重放能从精确来源重算 |
| E0：计量基础前置 | 完整请求估算、提前触发/硬拒绝、实际 usage、F2 中文设置 | 来源与模型绑定；精确 CAS、取消、超限不调用；真实服务逐请求误差对照 |
| D：语义摘要 | 宿主选择来源；扩展原 Provider 请求许可/计费/取消与请求重建合同 | 摘要来源冻结、真实模型摘要、约束/进度/未决问题保留、未知不变成功、摘要非 Memory 批准 |
| E：轮内预算与体验 | 基于 E0 的轮内自适应腾挪、逐类 token 资料取舍与超限交互 | 不拆活动工具组、不重复副作用、不无限压缩/重试；真实长旅程与切题验收 |

E0 已实现 A 的预算范围，包括系统提示、工具定义、Product 状态、Surface、Context reference、当前问题回显、
可见 Provider 封装和输出预留。窗口和编码显式填写，本地计数永远标为 estimated，实际 usage 单列；输入硬上限扣除预留与余量。提前触发默认输入上限的 80%，可调整为 1–100；60%–65% 目标未作为当前 E 合同的保证，E1 只处理闭合旧历史。未配置 E0 时保留字节触发；配置后 E1 在每个允许维护的 Step 按完整 token 压力调用同一 C/M3 owner，仍受阅读许可、回答余量和硬上限约束。

B 使用同一次 Effect Outcome 保存正文和引用；它不需要一个新的外部文件/垃圾回收体系。
目录查询不依赖整个历史 Turn 能装进一页，原 History 完整对话边界保持不变。

B+ 搜索一份已定位的 retained output，默认大小写敏感字面匹配，支持 content/data、附近行和
next_offset 翻页；片段不足再由 read_tool_output 展开。无命中不是语义不存在的证明，
相邻行不一定属于命中记录；模型须核对正文归属。当前不加独立全文索引或向量库。

所有阶段保持 append-only 事实源、人工 Memory/项目权限和 ProductTask 当前状态。后续模型摘要
必须有正式调用证据，不能作为不受管理的服务绕开预算和取消。C、E0 与 D 已按用户授权完成；E1/E2/E3 及语义检索复测已获用户明确目标授权，
不提前实现 Sandbox/MCP/新 Workflow，不提交或推送。

D 的配置、同 Turn 摘要 Step、失败/取消/恢复、显式 History 阅读优先级及协议变体见 [ADR-0057](../adr/0057-semantic-history-summary-step.md)。摘要自身必须通过硬上限，无分批或无限重试；默认规则摘录。
