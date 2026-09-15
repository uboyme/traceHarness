# 一次分工两个可写助手的真实验收证据

`summary.json` 是 2026-09-14 三次真实试次的精简证据，对应[记录 072](../../../deal/072-multi-child-real-acceptance.md)与 [ADR-0078](../../../adr/0078-multi-child-concurrent-allocation.md)。

题目 `multi-child-telemetry` 的 requirement **点名要求两个可写助手且路径不重叠**（`assistants_required_by_requirement: true`），所以这份证据只说明机制与交付，**不说明**模型会自发选择多助手，也不包含任何提速或质量收益。题面点名数量是隔离机制用的脚手架：产品侧数量由宿主 `coder.budget.max_children` 授权，计划内 1..N 由模型选择（结构上限 8，离线用例覆盖 N=1/2/3）。

三次试次全部保留：前两次因宿主配置被拒（时钟上限、进程槽），第三次机制全链通过但固定功能检查两次失败（助手 A 的 `normalize` 漏验顶层输入）。第三次共 25 次调用（主 17、子 8）、297928 exact tokens、254.9 秒，两份独立 Patch、两次成功整合、按 assignment 的完成回执齐全；无 Review 通过、无 Promotion；预算账户全关、工作区 1 释放 3 隔离留证、6 份沙箱证据收敛、27 份请求快照留存。

本轮还暴露并修复了三个宿主缺陷（整批预检维度不全、预算耗尽错误不记维度、多 assignment 回执读取时的变量遮蔽），详见记录 072。原始 SQLite、工作区与运行目录保留在本机忽略目录，不进仓库。
