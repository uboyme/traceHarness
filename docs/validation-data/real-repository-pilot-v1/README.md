# 真实模型试跑条件与派生结果

第一笔调用前固定的六份原 run-plan、顺序/源码/材料摘要、沙箱及 task settings。无凭据。
这些文件是条件审计副本，缺少完整材料树时不能独立直接执行；实际运行目录位于忽略的
`.traceh/rr-eval/live-pilot-v1/`，保留原 paired worker、EventStore、CAS、Git 与 report。

根目录保留原 v1 条件与两臂失败（10 个槽位未开始），`v3/` 为当前新冻结条件。v2 配对计划未发起真实模型调用；材料版本 2 的离线验收另有记录。
`same-candidate-material-v2.json` 是相同候选的离线检查，不算新增模型成功；cost-diagnostic.json 保留未知费用。
v3 两轮 12 次已结束；`v3/final-descriptive-summary.json` 按原报告绑定列出 12 行、阶段用量和
费用规划。`raw-eventstore-index.json` 保存本地 EventStore 的摘要，完整库及 CAS/Git 仍在忽略的
执行目录。预算额度不能写成供应商实际账单。
公开候选池仅为扩集排队元数据，新增可运行题数仍为零。过程见[记录 078](../../deal/078-real-model-repository-pilot.md)。

`v3/report-*-*.json` 是已结束两臂的原报告副本；`pair-*-summary.json` 与 `recheck-*-*.json`
分别是费用诊断和原 Reader/Session 请求重建的复核记录，通过报告摘要绑定。全量 EventStore、CAS、
Git 和失败工作区仍保留在本地执行目录，这个小型审计包不能独立替代完整原始证据归档。

`v3/execution-addendum.json` 记录首组后、第二组前开始采用的取消费用对账及短输出根规则，
原 preregistration 不回写；模型、额度与评分不变。`run-pilot-batch.py` 是原 CLI 的顺序调度，
`summarize-pilot.py` 只读取已经完成且绑定一致的原报告，生成逐题描述性表和阶段用量，
不会创建另一套成功判据。脚本需从仓库根用 `runpy.run_path` 运行，并显式传入运行目录与输出路径；
缺组或缺复核时拒绝生成完整汇总。用途是审计，不把三题重复当独立样本计算显著性。

两次原 single 失败发生在材料 v2 的固定断言前：候选改了未选中的普通测试模块。
`astroid-single-counterfactual.json` 与 `marshmallow-single-counterfactual.json` 分别记录只在
诊断副本还原该测试文件、保留源码补丁且保持原 Verifier 时 26/26、77/77 通过。
`material-v3-*.json` 和 `candidate-v3-*.json` 分别保存修正后材料准入、原 Product 主线对照与
原候选的新材料重验；这些不能改写 v3 原试次或证明多 Agent 收益。
