# 按需并发交接的一次真实观察

`summary.json` 是 2026-09-13 一次经用户授权的真实试次精简证据，对应[记录 071](../../../deal/071-concurrent-handoff-real-acceptance.md)与 [ADR-0077](../../../adr/0077-on-demand-concurrent-child.md)。

题目沿用 WC-4 遥测夹具的原文件、参考实现与固定检查字节，只有 requirement 文本不同：它声明 `telemetry_report.py` 可仅依据 INDEX.md 编写、不依赖助手代码。**requirement 没有出现任何交接模式名称**（`handoff_mode_named_in_requirement: false`），`dispatch_and_continue` 是模型自己选的。

上限：整树 32 次调用、600 秒、连接 60 秒、零重试、一题一次；预检已在固定镜像内证明空实现失败、参考实现通过。实际 15 次调用（主 11、子 4）、115656 exact tokens（无 unknown）、114.453 秒，无 Provider 失败、未触顶。

并发证据分两条独立来源：驱动记录的主/子 Provider 调用在飞区间重叠 **6.905 秒**；原账本派生的整树累计工作时长 121534 ms 比实际墙钟 114453 ms 多 **7081 ms**。两者都只说明模型调用在时间上重叠，不是 CPU 并行，也不是提速结论。

收敛与交付：Product/Workflow completed、固定功能检查通过、Review 通过、隔离目标推广提交；3 个预算账户关闭、17 笔使用预留与 2 笔子预留终结、3 个工作区全部释放（live 0）、3 份沙箱证据收敛、17 份请求副本离线重放通过且原库 SHA-256 未变。

限制：一题一次，没有同题串行基线，因此**不主张任何时延或质量收益**；推广只发生在隔离基准目标，不是项目仓库发布。原始 SQLite、工作区与运行目录保留在本机忽略目录，不进仓库。
