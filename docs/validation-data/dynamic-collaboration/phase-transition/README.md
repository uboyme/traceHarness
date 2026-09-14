# DA-10 阶段切换诊断证据

这是请求级归因实验，不是完整 Product 任务分数，不计入现有 benchmark 成绩。

- [summary.json](summary.json)：12 次真实调用的原摘要；提案未执行。
- [audit.json](audit.json)：原请求身份、各条件派生摘要、证据配对不变及原数据库不变检查。
- [合同](../../../plan/TRACEHARNESS_DA10_PHASE_DIAGNOSIS.md)与[解释](../../../deal/036-phase-transition-diagnosis.md)。

原输入为 DA-9 三类材料第一次独占决定请求。完整派生请求、响应和执行前冻结 plan 位于本机忽略目录 `.traceh/da10-phase-1`；这里的名字是一次实验定位，不是生产默认值。Provider 通过现有授权连接加载器直连，未保存连接秘密。没有执行返回工具、创建子 Agent、运行 Docker 或自动推广。

四条件各 0/3 有效决定；12 calls、44477 exact tokens、0 Provider failures。每格只有一次观察，不证明一般成功概率为零，也不能宣布模型永远无法切换。原有侦察证据不充分的限制保留；末尾重申同时影响位置和重复次数。
