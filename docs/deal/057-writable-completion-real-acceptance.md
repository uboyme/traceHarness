# WC-4 完成门禁后的第三次真实验收

2026-09-13。用户明确授权真实完整验收实施。本次完整执行了一轮有界验收，结果失败；WC-2/3 已完成，WC-4 尚未通过。记录 054/055 的失败和 056 的实现保持原样，没有追加第四轮调用。

## 冻结与执行

复用原 writable_acceptance 驱动、EvaluationRunner、Product、Supervisor、Workspace、Artifact 和 Sandbox。新目录为 `.traceh/wc4-completion-20260913/`，源码摘要与材料、驱动、沙箱、上限在调用前冻结。题目、参考实现和固定检查未修改；预检空实现 exit 1、参考实现 exit 0。

一题一次；主子最多 32 次、600 秒、单连接 60 秒、零 Provider 重试。现有角色预算仍生效，没有提高权限或安装、拉取镜像。隔离 benchmark 的批准策略只适用于其本地裸目标，不授权项目提交或发版；本次未到批准阶段。

| 项目 | 原事实与结果 |
|---|---|
| 真实调用 | 25：主方 20、助手 5 |
| Provider 报告 tokens | 248004，全部标为 exact；主方 221703、助手 26301 |
| Budget 结算 tokens | 236301；24 笔 exact、1 笔 unknown token 结算，另有 2 笔时间结算 |
| 耗时 | 284108 ms，约 284 秒 |
| Provider 失败 / 重试 | 0 / 0 |
| 子 Patch | 2152 字节，只修改 telemetry_rules.py |
| 主方完整读取 | 两页覆盖 0–2000、2000–2152；两页全文进入后续 15 份原请求 |
| 整合 | integrate_child_patch 确实 applied；完成检查核对同一交接和目标回执 |
| 主方自测 | 两次 python test_telemetry.py，均 exit 1 |
| 完成检查 | 原 Session seq 248、262：两次 passed=false，固定命令均 exit 1，候选未变化 |
| 返工 | seq 255 的冻结请求确实携带前次失败反馈；之后没有任何修复工具调用 |
| 最终状态 | Product / Workflow failed；无主方最终 Artifact、正式 Review、Approval 或 Promotion |
| 收敛 | 3 个账户 closed、27 个使用预留 settled；live 0、released 1、quarantined 2 |
| 重放 | 27 份请求副本重放通过：25 次真实调用、2 次脚本控制；原数据库 SHA-256 不变 |

Token 两列不是同一事实。原 BudgetTokenGuard 对一笔超出预留的报告按既有规则结清全部预留并标 unknown；不能用结算的 236301 冒充真实调用成本，也不能把全部结算称为 exact。本次保留 Provider 原报告的 248004，没有金额换算或收益比较。

## 失败原因和门禁证据

主方仍新增了要求禁止的 test_telemetry.py。完成检查的 delivery 清单明确列出三个路径，冻结检查因此失败。助手仍缺少最外层 list 校验：把保留模块复制到临时只读、无网络 Docker 中定向诊断，None 抛 TypeError，空 dict/tuple 返回 []，均违反要求 ValueError 的规格。诊断不修改原目录或评分，不算新的验收通过。

主方自测还写错了排序预期：规格要求 device 先 strip/lower，再排序；测试却期待 BDEVICE、ZDEVICE、adevice。保留实现实际返回 adevice、bdevice、zdevice，这一排序行为符合规格。主方把自测预期错误误判成实现排序错误，最后连续三次重复同一回答，口头说继续修复，却没有发出工具调用。

原请求证明反馈已送达，因此不是“失败反馈未接线”。两次完成检查都引用匹配的原 Sandbox outcome，execution、stream、receipt digest 一致且 converged。两次检查之间 candidate tree 和 Patch 摘要相同，失败后按原一次返工上限停止，未绕过人工权限。固定输出仍未向模型暴露；反馈提供退出码和当前文件清单，不提供私有检查源码或细粒度业务错误定位。这一反馈粒度没有促使本次模型修复，不能将拒绝成功冒充完成成功。

通俗例子：助手交来零件，主方这次确实验收并装上了；交卷时门卫发现多带了不允许的文件，退回一次。主方只重复“我会修”，没动手，门卫就停止放行。挡住坏交付已验证，自动把作业改对尚未验证。

## 交付和限制

本轮未修改生产源码、题目或评分；新增独立证据与本记录，同步正式/通俗上下文第 1、14.3.7、15 及当前限制，更新入口、导航、WC 计划和交接。机制中的真实读取、明确整合、完成拒绝、失败反馈和停止、资源收敛已有证据；功能与完整交付失败，收益未测量。

既有 056 定向门禁不冒充本轮新跑。本次只运行冻结预检、单轮真实验收、副本重放、离线语义诊断和文档检查；未跑全量、L2–L4、Wheel、安装、单多收益对照，未提交、推送或发版。既有未提交改动保留。

源码/驱动/材料冻结复核通过，新旧 dataset 逐项相等。两版 0–20 章节及 14.3.7 对应、相对链接、代码块闭合、秘密模式和 git diff --check 通过；没有把旧门禁数量当成本轮重新执行的测试数。

分享证据见 [summary.json](../validation-data/dynamic-collaboration/writable-completion/summary.json)。原始数据库摘要：`785cd1b5fff33ed9dc698306afb63306e2a747f198319251dd745298e27122b1`。旧两轮原库摘要同时复核未变。
