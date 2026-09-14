# 有界源码读取真实对照证据

范围与解读见[合同](../../../plan/TRACEHARNESS_BOUNDED_SOURCE_READING_CONTRACT.md)和[记录 029](../../../deal/029-bounded-source-reading.md)。这不是原 72 题或 DA 68 次实验的新分数。

- [summary.json](summary.json)：从原 Product 报告、Directory/Inbox、Session 成功请求回执派生十二个 trial 的费用、委派、资源收敛及实际可见读取页。页只保存元数据与内容摘要，不复制完整聊天。
- [source-review.json](source-review.json)：逐题 Codex 源码审阅、原 CAS 补丁摘要、反证源码位置；不是用户人工金标，不覆盖原结构评分。`patch: null` 表示未捕获产物，空补丁也不等同于合格输出。
- [checks.json](checks.json)：三批定向检查、反向验证、两臂冻结身份与严格重放摘要。
- `reviewed-patches/`：原 CAS 已校验读取的 11 个补丁副本，其中两个为空；没有补造第十二个不存在的产物。

本机原始材料在 `.traceh/ndm`，两臂证据在 `.traceh/r0`、`.traceh/r1`，源码/驱动/材料归档与冻结合同在 `.traceh/rr`。这些是本次诊断目录，不是软件默认配置。原版源码摘要 `ea37f0141702e88da53fda7e8923799780a27cc7db6231d46e4ceb25108e0e6a`；新版 `00cce85629b74d7f3f60a35dce88731878dfa4d8ebadf84055002777dfca6a67`。

重新运行必须显式提供获准连接、现有沙箱策略、冻结材料和全新输出目录，入口仍是 `python -m live_dynamic_collaboration.diagnosis`；不同阅读实现放在独立冻结源码路径，使用相同依赖。普通 pytest 不调用真实 API。复核已结束的证据使用原 `diagnosis_audit` / `diagnosis_evidence` 与各自冻结版本的 `live_optimization.reopen.replay`，不能用当前模块重标旧实验源码。

只保存公开连接类型及摘要，不保存真实密钥。两次初次重开清理 SQLite 临时副文件导致的断言失败与最终严格通过分别保留，详见记录 029；没有修改原数据库、报告或 CAS 来通过检查。
