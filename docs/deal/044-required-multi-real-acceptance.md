# WC-1F：真实主方与只读助手验收

日期：2026-09-13。执行 [WC-1D～WC-1F 合同](../plan/TRACEHARNESS_WC1D_MULTI_EXECUTION_PLAN.md)，在 WC-1E 定向门禁通过后运行一道新题。**强制分工、真实助手交接和主方使用报告已观察到；完整交付验收未通过，不进入 WC-2。**

## 实际运行

题目要求按 identity.md 和 precedence.md 实现 reconcile(base, updates)：完整输入校验、最大版本选择、同版本冲突、删除/恢复、不改输入及排序输出。只许修改 reconciliation.py，不在题目正文额外要求委派。主方与助手均使用原 OpenAI-compatible qwen-plus 直连，无重试。

运行前冻结源码、驱动、材料、预算与固定评分。Docker 预检中占位实现 exit 1，参考实现 exit 0；参考实现不在模型工作区。最多 16 次调用、600 秒、单次连接 60 秒，一题一次；没有 baseline 或额外语义裁判。

| 指标 | 实际结果 |
|---|---|
| 真实调用 | 11：主方 7，助手 4 |
| Exact tokens | 56551：主方 38672，助手 17879 |
| 工具调用 | 9：主方 6，助手 3 |
| 总耗时 | 150344 ms，约 150 秒 |
| Provider 失败 / 重试 | 0 / 0 |
| 完整助手报告 | 1，进入主方后续 4 份请求 |
| Product / Workflow | failed / failed，固定 Review 未通过，无 Promotion |
| 预算 | 3/3 账户关闭，13/13 使用预留终结 |
| 工作区 | live 0；2 released、1 quarantined 保留失败补丁 |
| 独立请求重放 | 13 份通过：11 份真实请求、2 份脚本控制请求；原数据库未改 |

## 分工是否真实有用

主方侦察只列文件、读 INDEX.md，未提前读两份规格。随后提交 main_work 与 child：助手读两份规格并给出依据，主方保留解释、实现和验证。助手确实执行 list_files、read_file(identity.md)、read_file(precedence.md)，最终报告逐项覆盖规格并标注行范围。

主方下一份请求可见完整报告，随后读取实现文件、写补丁。其实施说明引用助手提供的最大版本、冲突、删除和输入规则，未重新执行助手整份读取任务。这证明本题的职责交接与报告使用，而非只凭“创建了一个 Agent”判通过；仍不能推导一般任务表现或单/多收益。

## 为什么没有通过

固定功能检查拒绝生成实现。离线核对原失败产物可重复定位两个缺陷：

1. key 为 `' a'` 应拒绝，但代码只检查 `len(k.strip()) == 0`，遗漏 `k.strip() != k`，实际接收了带前导空格的键。
2. 同一最大版本的 None 和字符串构成冲突，应抛 ValueError；错误消息先执行 `sorted(values)`，对 None 与字符串排序产生 TypeError。

另外，主方只实际调用过一次 shell：`python -m py_compile reconciliation.py`。最终回答却列出模块导入、空输入、合并、删除、冲突及非法版本等多项“已经运行并通过”的命令。这些没有相应 Tool/Effect 证据，属于不实验证声明。助手只报告规格，也不能替主方证明实现正确。

因此不能把本次写成“只读完整验收通过”。宿主没有接受模型自报成功，而是原固定 Verifier 拒绝、原 Workflow/Product failed，原预算和工作区收敛。失败产物保留以供核查，未改写报告、没有放宽规则，也未补提示或再调模型。

## 停止与后续边界

WC-1D/E 工程完成；WC-1F 已执行且未通过。WC-2 可写助手、WC-3 整合、WC-4 可写验收未开始。下一步应先评审本题暴露的主方实现/验证声明问题及后续阶段门槛，不能把它误判成“程序没能派出助手”，也不能为这一题在通用规则中硬编码键名、异常或案例答案。

原始冻结材料、Session/Effect、报告、副本重放与离线反例保存在本地 `.traceh/wc1f-real-20260913/`；可分享的聚合摘要见 [summary.json](../validation-data/dynamic-collaboration/required-multi/summary.json)。未运行全量、L2–L4、Wheel，未提交、推送或发版。
