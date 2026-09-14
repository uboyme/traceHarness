# 未读来源分工：一次真实请求观察

2026-09-13，用户授权沿用记录 048 的原分工请求，只替换 ALLOCATION_GUIDANCE。**本次提交合法分工，不再以“未读规格”为由拒绝；尚未执行助手或完整任务。**

## 冻结及结果

原请求来自记录 047 的关闭数据库，按 Stream/Attempt/Step/Turn/snapshot/fingerprint 核对。只替换 system_prompt 和唯一工具 description 中的分工说明；schema、消息、模型、温度、输出上限和其他字段不变。独立冻结候选请求/源码摘要，原连接直连，一次调用、60 秒超时，无重试，不执行 Tool。

| 项目 | 结果 |
|---|---|
| 真实调用 | 1，qwen-plus |
| 用量 | 2616 input + 1216 output = 3832 exact tokens |
| 耗时 | 27295 ms |
| Provider / 参数 | 正常解析、合法 JSON object，原工具 schema 通过 |
| 分工调用 | 恰好一个 submit_collaboration_plan |
| 主方工作 | 设计、修改目标文件、实际验证、汇报 |
| 助手工作 | 读取两份已定位但未读规格及当前实现，报告事实和局限；不写、不运行代码 |
| 离线复核 | 原 Provider 对保存响应再次解析一致；严格字段及长度通过，payload 摘要一致 |
| 执行与原记录 | 零 Tool 执行，原数据库不变 |

本次响应明确区分 identified but unread，直接安排助手读取，不再把主方不知道细节视为不能委派。主子没有重复承担整个任务，计划未申请写权限、代码执行或递归助手。该观察支持说明方向，但单次不证明稳定提升，旧参数失败也不能由此追认解决。

## 仍需关注的交付设计

模型要求报告提供首尾各 5 个非空行及结构观察，这种要求在长文档中可能丢失关键中段；本题短文件不据此直接判定实际遗漏。scope 的原文要求与 deliverable 的摘录要求也不够一致。它还把助手报告称为 sole authoritative source，措辞错误：原文件是来源，助手报告是带证据的主张，原宿主合同仍要求核验。这些不能当成宿主真的授予了新权威，但完整验收应检查它是否依赖不完整摘录或未经核验的主张。

结论分开保留：合法分工及“未读可委派”的请求级观察通过；实际报告是否完整、主方是否正确使用、验证与收尾仍未测。没有为此次结果继续改提示或追加调用。

本地原始证据在 `.traceh/unread-allocation-probe-20260913/`；分享摘要见 [unread-allocation.json](../validation-data/dynamic-collaboration/verification-review/unread-allocation.json)。两份上下文同步当前状态、第 14.3.1、15 节；本轮无生产改动、全量、L2–L4、Wheel、提交或发行，未开始 WC-2。
