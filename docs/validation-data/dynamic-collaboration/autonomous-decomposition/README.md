# 不点名数量时的自主拆分证据

`summary.json` 对应[记录 073](../../../deal/073-autonomous-decomposition-probe.md)。题目 `autonomy-telemetry-pipeline` 的 requirement **全文没有任何数量字样**（`assistant_count_named_in_requirement: false`），宿主授权 4 个助手，任务含 4 个各自独立成节的模块加一个组合模块。

两次真实试次中模型都自主拆出 **4 个 assignment**，与独立单元一一对应、文件互不重叠，并把组合模块留给自己；4 份独立 Patch 全部捕获并被显式整合。第一次主方自身 token 耗尽于固定验证之前，第二次（仅放大主方额度）走到固定验证并两次失败——助手未校验顶层输入。

边界：当前 multi 模式本来就要求分工，所以这只说明**拆分粒度**，不说明模型会不会主动协作；两次观察不是比率；没有单助手基线，因此**没有任何收益结论**；没有推广。原始 SQLite、工作区与运行目录保留在本机忽略目录。
