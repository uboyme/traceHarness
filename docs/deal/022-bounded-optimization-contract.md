# 022：AO-0 受限策略合同

日期：2026-09-10。完成 AO-0；未开始 AO-1 调度器或 AO-2 真实策略实验，未提交、推送或发布。
实施前冻结了 [AO-0 合同](../plan/TRACEHARNESS_OPTIMIZATION_AO0_CONTRACT.md)，依据原 ADR-0066。

## 所属层与实现

此前 evaluation 已有受限文本 patch、独立 worker、证据核验和 comparison；缺少的是优化策略的窄接口、
每轮允许范围、提案身份/去重及停止规则。因此本次在 `api/optimization.py` 定义纯 DTO/Protocol，
在 `evolution/optimization_contract.py` 实现校验，不把优化职责塞进 AgentLoop。

`traceh.api` / `traceh.plugins` 同源导出 strategy/analysis 与请求/结果类型。
策略服务由插件 provide，分析服务由宿主装配后 require 借用；原 Service/Scope/Generation Lease 继续管理身份和生命周期。
宿主负责开发失败数据的选择与脱敏，接口不自动清洗任意正文，不传 scorer、留出答案或用户会话。

合同绑定完整源码、benchmark、run plan、development dataset 和 case IDs、确切可改文字、九项额度及 UTC 截止时间。
validate_request 核验请求与当前源码；admit_proposal 核验本轮范围、旧文字、实际改动、请求绑定，
最后交原 `evaluation/variants.py` 的同一个 AST 校验/改写入口。原七个允许字符串节点没有扩张。
按排序后的确切 patch 摘要去重，理由或编辑顺序不算新候选。输出只是可交 evaluation 的 patch，不授予采用权限。

decide_next 从原记录的派生进度判断继续、等审阅或停止；有待审不能继续试或算收益，未知成本、
未证明收尾/证据、取消/期限、无候选、上限或整批预留不足都不能继续。
这是新轮次前的纯检查，不是资源预留器、评分器或第二账本；AO-1 必须核验原工件、预留完整批次后执行。

## 实际验证

离线 **105 项不重复测试通过**：

- 新 `test_optimization_contract.py` / `test_optimization_services.py` 共 53 项。
- 原 `test_plugin_sdk.py` / `test_plugin_activation_sets.py` 和 evaluation comparison、comparison_evidence、architecture 相邻回归共 52 项。

正向提案使用当前真实源码，经原 UE-3 apply_candidate 后逐文件核对 AST，只有指定字符串改变。
关键反例使用 UE-3 全局允许、但本轮不允许的真实 Memory 说明节点；旧值摘要和候选本身都合法，
仍必须因本轮范围被拒绝，避免“只因夹具坏了而碰巧拒绝”。
其他反例包括错请求、旧基线/旧文字、评分器/schema/路径越界、重复节点、无改动、超长文本、
开发案例越界、非 tuple 输入、未知格式、无界限额、待审、各上限及整批预算边界。

服务测试走真实 Runtime/PluginManager/Generation：手工策略提案接纳、在途分析中替换策略但旧 Lease 保持可用、
分析失败传播、重复取消等收尾、setup 在注册后失败/取消撤销服务、缺少宿主分析服务明确激活失败。
分析实现为明确脚本替身，借用/失败/收敛用 Event 和原 await_worker_convergence 核验；不是 AO-2 Provider 的真实验证。

反向验证临时修改当前 owner 后恢复原字节：

| 移除的保护 | 实际结果 |
|---|---|
| 将本轮范围放大为 UE-3 全局允许范围 | 真实 Memory 节点越界提案被错误接纳，范围测试 DID NOT RAISE |
| 关闭确切 patch 去重 | 换理由/顺序和新请求历史两项测试 DID NOT RAISE |
| 跳过 pending_review 屏障 | 测试观测到 continue，预期 await_review，失败 |

三种变异触发四个预期失败；源码恢复后 53 项新定向测试再次全部通过。
compileall src/tests、七个本阶段源码/测试文件 Ruff、全仓 collect-only 3915 项、例子硬编码扫描通过。
collect-only 仅收集。没有运行全量 pytest、L2–L4、Wheel、联网包索引或真实 API。

## 文档与交付边界

先更新正式上下文，再以其为准同步通俗版：1、3、12.7、13、14、15、17、19；12.6 的末尾阶段状态也同步，
新增 Mermaid 图区分当前合同与 AO-1 尚未接入的调度线。同步执行设计、Plugin SDK 作者说明、README、CHANGELOG。
链接、Markdown/Mermaid 代码块、两版 0–20 编号、秘密形态与 diff 检查均通过。
前面 UE-4 的原始工件、人工 pending_review、成绩及真实请求数量不改写。

通俗例子：现在允许优化模型递交“把 Skill 搜索说明讲清楚”的建议；擅自改权限会被拒绝，
同一改动换个理由再交会被识别为重复，结果还等人审就暂停。真正自动安排试验是下一步 AO-1。
当前没有真实优化策略/分析 adapter、自动候选选择、自动采用或新 CLI；不声称检索效果因此提升。
