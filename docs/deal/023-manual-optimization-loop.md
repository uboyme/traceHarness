# 023：AO-1 人工候选走原评估闭环

日期：2026-09-11。范围以 [AO-1 合同](../plan/TRACEHARNESS_OPTIMIZATION_AO1_CONTRACT.md) 为准。
未开始 AO-2，未提交、推送或发布；不运行全量、L2–L4、Wheel 或外部模型 API。

## 实现与职责

AO-0 已有受限提案及停止规则，本轮补上开发控制面的执行者 `evolution/optimization.py`。
人工文字候选先绑定请求、经过原准入和 AST 检查，再交原 EvaluationRunner / 两臂 worker。
没有修改原七个可改节点、AgentLoop、检索策略、评分器、权限或执行事实协议。

- EvaluationRunner.for_plan 重用原 evaluator 的构造条件；原 comparison.inspect_experiment 抽出
  同一核验/比较计算，写报告和优化判断都消费它，不复制成功标准。
- 全新目录独占写入；冻结合同、源码、原计划/材料/输入、环境、完整试次及每轮整批预留。
  原评价工件及 Session/Effect/Product 等账本继续拥有执行事实，优化层只记录定义和执行引用。
- 正式可判、完整证据、零质量损失、明确门槛通过且有改善，才能交后续审阅。没有自动采用权限。
  无收益和重复分别计数；待审退出；未知用量、错误来源、失败或超时保留原状并停止。
- 离线重算可以绑定确切的原 assessment manifest，不使用缓存分数，也不恢复剩余候选。
  不支持旧目录接管/冷恢复；新执行使用新的显式实验。
- 取消和总截止等待原子进程收尾，重复取消不提前返回；执行与收尾/报告落盘错误同时保留。

## 可核对的实例

`tests/test_manual_optimization.py` 的显式夹具完整运行当前生产主线：

1. **检索旅程：** 人工修改一段导航文字，启动两个不同 PID 的原 worker；从原 SQLite 请求快照
   确认新文字只进入候选臂。两条语义评分都待审，下一候选不执行。用明确标注的人工评分夹具走
   原 review/assess 后只读重算；这是审阅协议测试，不冒充真实人工签字。
2. **Product：** 在已有本地固定 Docker 镜像中执行原验证，真实 Git/工作区/补丁/推广账本收敛。
   队列含候选 A、重复 A、候选 B、剩余候选；A/B 都正式通过但相对原版无收益。重复 A 不执行，
   连续两次无收益后停止：共四个正式 trial，保留重复拒绝记录，不再运行剩余候选。
3. **失败：** 本地 HTTP 实际收到请求后断开，保留真实失败尝试及未知费用；不伪装成质量退步。
   取消/总截止测试先等待请求到达，再触发取消，核对原 process 回执中进程已退出、无强杀。
   报告写入再失败时，原取消与 I/O 错误都存在，不生成假成功报告。
4. **边界：** 错源码/题目/原计划、缺少比较门槛、整批额度不足、非法/越界建议、错运行身份、
   被改写的原评估报告均有拒绝/停止检查；改缓存 report 不能改变结果。

回复使用确定性 Scripted Provider 或本地 HTTP 替身；这些是真进程、真原账本、真 Git/沙箱验证，
**不是外部优化模型效果实验，不产生新检索正确率或新的 72 题成绩。**

Windows Git 验证沿用原 Product 测试的短临时目录。首次在长 pytest 目录运行时出现
workspace-git-failed，原任务未完成且调度停止；改用原测试的短目录后完整主线通过。
这不承诺任意深度 Windows 路径可用，也没有放宽 Git/收尾门禁。

## 验证记录

当前阶段定向集合为 `test_manual_optimization`、`test_optimization_contract/services`、
`test_evaluation_comparison/comparison_evidence/workers/variants_product/architecture/lifecycle`、
`test_product_benchmark`。新闭环与原相邻 owner 共同检查，不执行其他全量/L2。

该集合 **142 项通过**：新增人工闭环 22 项、相邻 owner 120 项，耗时 486.78 秒。
最慢项为新 Product 四试次（140.27 秒）及原 Product 双臂回归（68.32 秒）；没有启用 xdist。
末次让坏证据/不完整核验的 Progress 明确标为 unproven/unknown 后，受影响子集 21 项复核通过；
数量按不重复用例报告，不把重复运行的检查累加成新测试。

反向验证逐次替换当前源码中的一个保护，执行对应公开入口测试，再恢复原始字节：

| 移除的保护 | 可观察的预期失败 |
|---|---|
| pending_review 判定 | 两臂已真实执行，却不再返回 await_review，原待审断言失败 |
| outcome 对原 experiment 的确切身份核对 | 换错运行摘要仍被接受，原拒绝断言失败 |
| 零质量损失要求 | 便宜但质量退步的候选被交审阅，原 not-qualified 断言失败 |

三项变异各产生一个预期失败，无夹具导入/类型错误；之后恢复原字节。
compileall src/tests、五个改动 Python 文件的 Ruff、硬编码扫描、collect-only 3937 项、
七份文档编号对应/相对链接/代码块闭合/秘密形态与 git diff --check 通过。
collect-only 没有执行全量，Docker 使用显式已有镜像和连接，没有拉取或构建镜像。

## 文档和后续边界

正式版先于通俗版同步 1、3、12.7–12.8、13、14、15、17：当前阶段、目录职责、运行/待审/停止、
输入与使用方式、扩展边界、验证和变更影响；新增 Mermaid 图，旧“尚无调度器”说明改为当前事实。
同步 README、CHANGELOG 和统一设计。AO-0 原 API/Plugin SDK 的服务身份没有再变更。

AO-2 才接真实策略/分析服务及未见留出实验。旧 UE-4 工件、pending_review 和历史成绩保持原状。
本阶段只是证明“建议能按同一把尺子完整试验并可靠停下”，不是宣布任何候选已经值得上线。
