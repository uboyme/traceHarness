# 自主委派诊断证据

[记录 028](../../../deal/028-autonomous-delegation-diagnosis.md)解释结果和限制。
[summary.json](summary.json)按原运行报告/Session 派生，保留两版全部 12 个试次；它不改变原结构评分。
[source-review.json](source-review.json)记录 Codex 对六份结构通过复杂答复的源码核读，未经用户金标确认；`reviewed-patches/` 是通过原 LocalArtifactCas 校验后导出的原补丁字节，不是重新构造的模型答案。

本机原日志位于显式实验输出 `.traceh/nd1`、`.traceh/nd2`，源码/候选归档位于 `.traceh/nc`，材料位于 `.traceh/ndm`，独立重放回执位于 `.traceh/da-independent`。这些是本次研究产物的位置示例，不是 Runtime 默认目录；摘要中的本地路径只用于定位，不代替哈希和原账本身份。

两臂各 75 次真实 API、87 份含 requester 的冻结请求；合计 150 次真实 API / 174 份请求 / 1,748,884 tokens。无失败 Provider 请求，不代表所有 Agent 成功：四个助手和一个原版主方因预算耗尽失败。候选未采用，默认 single 不变。
