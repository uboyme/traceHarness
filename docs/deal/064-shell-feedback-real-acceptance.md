# WC-4 命令反馈修复后的真实完整验收

2026-09-13，用户明确授权开始验证。使用当前配置 deepseek-v4-flash，主子相同；新目录冻结一题一次、最多 32 次调用、600 秒、连接 60 秒、零重试。空实现预检 exit 1，参考实现 exit 0，复用原 Evaluation/Product、Sandbox、固定检查及隔离 benchmark 批准策略。

## 验收结果

本轮冻结的 WC-4 完整链通过。15 次真实调用（主 11、子 4），114367 exact tokens，147875 ms；无 Provider 失败或重试。主方先调查、批量读三份文件并分工，助手提交 telemetry_rules.py 的 1397 字节 Patch；主方一次完整读取、显式整合 applied，再实现 telemetry_report.py。最终 Artifact 2610 字节，只含这两个文件，保留助手实现。

主方一次直接 python 命令完成两个模块的断言自测，exit 0，没有 start-failed、未提供工具或被拒调用。自测脚本包含按注释组织的 15+8 组场景，最终报告引用真实 tool_call_id 和退出码。随后原完成门禁通过，固定 functional-contract exit 0，正式 Artifact Review passed，Product/Workflow completed，原 Promotion 完成。

Promotion 仅作用于本轮一次性隔离 benchmark 目标，不是本项目的提交、推送或发版，也不是模型自行批准。现有人工批准边界未改。

## 证据核对

原完成检查核对整合来源和候选树不变；原固定检查同时覆盖功能与文件范围。静态核读最终 Patch 与题目一致：最外层 list、元素 dict/键集合、字符串、bool 排除和整数范围均验证；主方调用 telemetry_rules.normalize 后分组、排序并统计严格大于阈值的数量。

3 个预算账户关闭、17 笔预留结清；15 笔 exact、2 笔无 usage，账本与 Provider 总量一致。3 个工作区全部 released、live 0，无隔离残留。原 reader/CAS 核对自测、完成验证及 Promotion 的三份 Sandbox 证据，全部 finished、exit 0、converged。17 份请求在数据库副本中重放及不变量检查通过，原库未变。原报告送达指标本轮为 1，不据此改写旧失败轮的观测缺口。

- 冻结合同 SHA-256：`f1dbfb5f16093c2a88b48c8d6d56fa3ac54710cc56a09b624186520987ddc08d`。
- 原数据库 SHA-256：`805bcc3316e6f3a019e62185469c2e70e4c2116c0e5ab00a97a19e1d90d7baf0`。
- 脱敏统计见[结果摘要](../validation-data/dynamic-collaboration/writable-shell-feedback/summary.json)。本地保留本轮冻结材料、原日志和核验副本；前六轮失败原库均不变。

## 限制与交付

通俗例子：助手交作业，主方看完并合入，写完自己负责的部分，再实际测试，最后宿主按原标准验收——这次整条流程走完了。

这是一道冻结任务的一次成功，不能证明所有任务可靠，也没有单/多收益对照。没有启动失败，因此本轮验证了正确用法，但未观察真实模型收到新错误消息后会如何纠正；该路径的工程证据仍属于记录 063。主方自测的“新 dict”检查单独不足以证明不共享对象，原固定验证另有对象身份断言，不能把自测描述当作完备证明。

本轮没有修改生产代码、评分或权限；既有 70 项定向门禁不冒称重新运行。冻结源码/材料复核、文档章节/链接/代码块与 diff 检查通过。正式与通俗上下文同步当前状态、14.3.13、验证基线和限制，入口、WC 计划和交接同步本题通过。没有追加模型调用、全量、L2–L4、安装、项目提交、推送或发布。
