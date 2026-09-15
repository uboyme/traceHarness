# 优先复用证据的收尾与一次有界错误恢复验证

2026-09-13，用户同意修复重复收尾提示，并提高下一次单项真实试验的额度。此授权只涵盖一项 command-recovery，不重启记录 066 的旧批次。

## 修改与原因

Product 的 delivery review 提示先要求复用已有通过且代码未变的检查证据；进入检查点本身不要求重读或重测。只有缺失检查或后续修改使检查失效时才补测。REVIEW 不再重复 EXECUTE 的助手交接说明。最近八条原工具回执、候选观察、失败结果、固定完成验证和人工 Promotion 保持原 owner，没有新增事实源或自动判定“测试足够”的算法。

这是一项指导修正，不强制模型按固定步数工作，也不保证模型始终遵从。提示正文由 2275 字符变为 2247 字符，并另外去掉重复交接段；这不是实际 Token 节省量。

## 本次冻结

显式测试驱动 closure_acceptance 复用原 writable_acceptance、Evaluation/Product/Supervisor/Workspace/Artifact。新材料明确 normalize 返回 sku/qty，且要求不可用旧命令只尝试一次，再根据反馈自行验证。

任务根 Token 上限 480000，coder 子树 360000，助手预留 30000，因此主方可用 330000。仅修改本试验配置，生产默认额度和权限不改。最多 32 次模型调用、600 秒单试次、60 秒连接、零 Provider 重试；原主方 300 秒、助手 120 秒 Turn 限额仍可先触发。仅一次，不自动补跑。

## 验证

39 项定向/相邻检查全部通过，覆盖既有回执直接收尾、缺失/失败检查、当前候选变化、固定拒绝与返工、取消收敛、身份和架构。恢复旧提示的公开 Product 测试因新提示送达断言失败；这验证提示接线，不能证明真实模型一定遵从。compileall、4215 项仅收集、5 文件 Ruff、文档与 diff 检查通过。新材料在既有 Docker 镜像中空实现失败、参考实现通过，未拉镜像。

## 唯一真实试次：固定检查拒绝，返工未完成

DeepSeek V4 Flash 实际调用 24 次（主 20、子 4），总耗时 315593 ms。23 次有 exact usage，已知合计 236088 tokens；最后一次调用取消，usage unknown，因此完整实际 Token 总量未知。账本结清 351143，包含未知使用的保守记账，不能当作 Provider 实际用量。

1194 字节助手 Patch 完整读取一次并显式整合。旧命令只试一次，收到 FileNotFoundError 后换用 Python；期间 find 命令误用重定向，随后纠正。主方自测 exit 0，首次 REVIEW 直接引用原调用交卷，没有重跑自测。此前代码未改变。这是所希望的收尾行为，但提示、预算、材料同时变化，单次不能隔离因果或证明节省。

原完成门禁核对整合成功，但 functional-contract exit 1。助手 normalize 缺少顶层列表校验；主方自测覆盖元素而漏掉容器类型。对原 Patch 的离线副本复现：None 抛 TypeError，空 dict/tuple 返回 []，均违反题目要求。固定验证原始 stderr 不保存、不向模型公开，不能伪称已读到原失败堆栈；这个反例是独立复现的代码缺陷。

收到失败摘要后，主方查询 functional-contract 的 retained output、检查 imports、尝试不可用的 git、读取属性文件，并再次搜索输出，没有编辑或补上顶层反例。第 24 次调用被取消，主方 Turn 最终 cancelled，Product/Workflow failed，无最终 Artifact Review 或 Promotion。配置仍为主方 300 秒；主方记录 309353 ms（含收敛），与原墙钟取消路径相符，不是上次的 Token admission 拒绝。

26 份请求副本重放与不变量通过；3 个账户关闭、26 个预留结清，工作区 released 1/quarantined 2/live 0，10 份 Sandbox 证据全部收敛。原库 SHA-256 为 `05fcc3fe1341714c192ba345fc5401ba615fd080576b84a2da26afbbf7bee0b2`，核验后不变。结构化摘要见[验证数据](../validation-data/dynamic-collaboration/closure/summary.json)。

当前限制：收尾重复测试有所改善，但漏验和固定失败后的定位仍不可靠。下一步应审视原完成反馈如何提供安全、可操作的失败定位，不应只继续增加预算，也不能直接公开私有验证器或另建事实源。本轮没有实现该后续设计，没有补跑真实模型，冲突练习仍未运行。

未运行全量、L2–L4、安装或发布门禁；不提交、推送或发版。旧批次和原数据库保留。
