# 按改动范围读取上下文

一次分工多个助手见正式版 14.3.20 与 [ADR-0078](../adr/0078-multi-child-concurrent-allocation.md)；宿主向模型隐瞒已知信息的三处修正（提示按工具组装、拒绝消息可纠正、`read_file` outline 模式）见正式版 14.3.26；大型代码库理解任务的七轮对照见正式版 14.3.27 与[记录 076](../deal/076-large-codebase-comprehension-rounds.md)。预算预留的可证明上界、超支拒绝与 Profile 必填的 `token_estimate` 见正式版 14.3.25 与 [ADR-0080](../adr/0080-bounded-token-reservation-and-overage-refusal.md)。派发调用的等待时长与单次工具调用上限的关系见正式版 14.3.24 与 [ADR-0079](../adr/0079-authorized-child-report-wait.md)。大型语料下主 Agent 上下文单调增长的现状核查、三轮真实对照数据与候选方案见[大型代码库上下文设计提案](../plan/TRACEHARNESS_LARGE_CODEBASE_CONTEXT_DESIGN.md)——该文是**提案，未决策、未实现**，不构成当前合同。记录 073 证明题面不写数量时主方两次自主选择 4 个助手；记录 074 的同题对照表明这类小模块任务中 multi 成本高于 single；[记录 075](../deal/075-autonomous-multi-child-tui-acceptance.md) 已走真实 PTY/TUI，四个独立助手身份、对话、Patch 和整合回执可见，但固定功能检查 exit 1，未进入人工审批或推广。仍默认 single，TUI 新建配置默认 `coder.budget.max_children=1`；只有宿主显式调大后模型才可在上限内选择多个助手。项目未提交发版。

先读[项目入口](project-overview.md)。下表章节号属于[正式上下文](project-context.md)；通俗版按同名主题核对，历史子章节编号并不全部相同。表是读取索引，不是替代正文的模块合同。

只读取选中小节到同级或更高层级标题之间的完整正文，包含其下子节。先用 `rg -n '^#{2,4} ' docs/note/project-context.md` 找标题，再按范围读；工具截断时继续分段，不把输出截断视为读完。不必为了定位小节而打印整本文件。

| 修改 owner / 源码目录 | 首读正式章节 | 必查相邻边界 |
|---|---|---|
| `runtime` 执行与 continuation | 4、5、10 | 6/7 请求与事件；11 取消恢复；Provider/Tool 的实际调用端 |
| `session` / Store / SQLite | 5、6、7 | 11 恢复，12.1 不变量、12.4 重放，以及当前事件的写入和消费 owner |
| `llm` / Provider | 8 | 7 冻结请求、5 Attempt、11 副作用恢复、预算预留与失败 usage |
| `tools` / sandbox | 9 | 6 Effect/Result、11 取消恢复；工具实际访问的 Workspace/文件/网络边界 |
| Context / Skill / Memory / retrieval | 7 中对应主题、12.2 压缩 | 原领域 Reader、权限/来源、Session 协议、请求重放；模块定位见 3、17 |
| `plugins` / `kernel` | 19 对应主题、4 装配 | Generation/Lease/Drain、owned task、Runtime dispose、受影响能力 owner |
| `agents` / `supervision` | 20.1–20.5、20.8–20.9、20.11–20.15、20.17 | 5 Session、20.20–20.22 预算/工作区；若改协作再读 14.3（并发与多助手见 14.3.19–14.3.20） |
| `budgets` | 20.20–20.21 | Provider/Tool 预留结算、Supervisor 所属树、取消、未知 usage |
| `workspaces` | 20.22 | Supervisor dispose/release 分离、20.23 capture gate、Git 和工具访问策略 |
| `artifacts` | 20.23 | 20.22 工作区、Inbox/Delivery/Session 静止点、CAS、20.24 推广 |
| `promotion` | 20.24 | Artifact identity、固定验证、完整批准绑定、Git ref CAS、Workflow 恢复 |
| `workflow` / `product` | 20.25–20.29、14.3 当前协作 | Supervisor、Budget、Workspace、Artifact、Promotion；只读 UI 不推进业务 |
| `evaluation` | 12.5–12.6 | 对应 evaluator 的原执行 owner、冻结输入/两臂隔离、证据与费用、审阅和比较 |
| `evolution` / 后台优化 | 12.7–12.11、14.2 | 原 Evaluation/Plugin Lease、宿主关闭、候选边界、人工采用；不改评分合同追高分 |
| CLI / TUI / chat | 13 对应主题 | 实际控制面与只读投影；配置解析、恢复、生命周期；按需要读 12.3 |
| 文档与开发约定 | 0、18 | AGENTS、入口、此表、受影响主题及其通俗解释；不顺便启动业务阶段 |

进一步定位具体文件和测试使用正式版 §17 变更影响矩阵。§15 是验证记录，不是每轮都要重跑的测试清单；§16 的相关限制必须读。DA 历史实验主要在 §12.15–12.23，当前 multi 合同在 §14.3，不能混用。

跨域任务取相关行的并集并去重。不得只读最上层 Product 而漏掉底层写入 owner；若发现未知依赖，再补读该 owner，而非把导航当成不可扩大的白名单。

## WC-2～WC-4 的直接入口

读[交接合同](../plan/TRACEHARNESS_WC2_WC4_HANDOFF.md)和[执行计划](../plan/TRACEHARNESS_WRITABLE_COLLABORATION_PLAN.md)后，按阶段选取：

- WC-2：14.3（显式写助手见 14.3.2）、20.22、20.23，及 Supervisor/Inbox/Delivery 的身份、claim、收敛小节和 Budget 装配；关闭重入收敛见 20.15。
- WC-3：上述产物合同加 9、11、20.24；检查 Tool Effect、目标前像、回滚、部分结果对账。
- WC-4：12.5–12.6、20.25–20.29，复核前两阶段实际合同和固定 Verifier/Approval，不再重新阅读全部 DA 试验史。

导航文件只维护路径和读取关系。需要新增合同，请写回原正式章节并同步通俗版，不在此表另写实现细节。
