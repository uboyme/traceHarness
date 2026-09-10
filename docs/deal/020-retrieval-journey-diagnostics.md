# 020：UE-3+ 检索旅程诊断

日期：2026-09-10。当前范围为原检索评估器、报告及离线审阅；未进入 UE-4/AO，未提交或发布。
实施范围见 [UE-3+ 合同](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_PLUS_CONTRACT.md)。

## 修改与原因

原判据已经能核对派发证据，但报告不便于区分“来源没出现”“找到目录却没读到正文”“正文到了但答案待审”。
这属于 evaluation 的派生观测层，不是索引、Context Composer 或 AgentLoop 的策略问题。
新增 episode_diagnostics，从原 Session 事件、成功请求和既有证据推导 candidate/evidence/gap；
episode_assessment 抽出共用的成功 Attempt 和实际 Context 派发核验，避免评分与诊断两套绑定规则。
live report 和原 review/assess 共用诊断函数；离线输出 diagnostics.json/md，保留原 trial assessment 和 provisional 匹配。

候选是来源容器粒度，Skill/Memory 用明确 ID，History 用原 Reader 的叶引用，Output 用准备轮 Effect/digest。
目录出现不等于正文；accepted 读取回执不等于正文派发；完整有效搜索片段已足够时不强迫 read。
原调用参数、成功失败、版本、章节/页引用、搜索字段/扫描量/游标和输出字符范围均可核对。
负例的正向指标为 not_applicable，保留原回答与实际范围；不靠否定词规则自动打分或强制遍历。
没有完整相关性标注，不增加完整 Recall/MRR 或消费率门槛。gap 是缺口观测，不直接断言根因。

原运行、评分绑定和源码归档保持不动；新诊断绑定原 run/frozen/evidence/report 与当前分析器 version/source_digest。
旧运行通过同一现行协议重新推导，不增加历史格式兼容、迁移、第二事实源或评分器。
没有修改用户配置、Runtime 提示、权限、事件协议或数据目录，也没有模型可见的新诊断内容。

## 验证

| 范围 | 结果 |
|---|---|
| episode_diagnostics、episode_dispatch_binding、episode_review_contract、retrieval_episode_evaluator | 41 passed |
| evaluation_comparison、comparison_evidence、evaluation_architecture、product_architecture | 46 passed |
| 派发绑定反向验证 | 临时绕过实际消息核验后，未派发目录被误记 observed，新测试按预期失败；恢复后 1 passed（前组重叠） |
| compileall src/tests；修改范围 Ruff | 通过；46 个源码/测试文件 |
| pytest --collect-only | 3858 tests collected；没有执行全量 |
| 归档源码实际 CLI 重导 | 两臂 diagnostics.json 分别与归档一致；无新增 Provider 调用 |
| 文档与产物 | 两版 0～20 章对应、相对链接、代码块闭合、秘密形状检查、归档摘要与 JSON/Markdown 一致均通过 |
| 反硬编码及差异检查 | 技能扫描无命中，通用代码无实验故事/模型/种子/本机路径；git diff --check 通过 |

87 项互不重复的相关测试通过；前期小批次与恢复检查有重叠，不累加。检索组包含原真实 Docker shell 输出准备、
搜索和读取；失败/错章节、完整搜索片段、失败 Attempt、取消后无 packet 和人工评分更新均覆盖。
取消测试通过确定性 Provider 替身的 Event/Gate 确认实际调用已开始后触发，并等原 owner 关闭；不把替身说成真实模型 API，也没有用未执行的空路径通过测试。
反向验证仅在测试进程内替换检查函数，原运行真实执行后才移除派发消息；源文件、原存储和临时保护没有遗留修改。

## 原真实轨迹离线复算

原 UE-3 八对 16 条 qwen-plus 旅程全部重新派生诊断：8 个正例均有来源和足够派发证据；
8 个负例保留范围审阅，全部 16 条仍 pending_review；没有新质量得分或胜出结论。
原实验 719 个文件哈希前后一致，新分析器源码单独归档，JSON 和 Markdown 完整对象一致；新增 Provider 调用为 0。
归档含分析器 302 个 Python 文件及摘要，使用归档源码经实际 CLI 重新导出两臂，结果完全一致；Git 按原字节保留统一评估证据。
复核示例起初误写为 `python -m traceh`，实际调用明确报无 __main__，现已修正文档并用有效的 `python -m traceh.cli.main` 重导通过；未为示例添加新入口。
第一轮诊断后补全原分页/章节定位信息再离线重算，没有重新运行模型或筛掉失败样本。
已有 UE-3 的 102 份请求重放结果仍是原阶段记录，不冒充本轮新 API 或新重放次数。
结果、原回答、两个通俗例子和复核方法见 [离线诊断证据](../validation-data/unified-evaluation/ue3-plus/README.md)。

## 同步与停止边界

正式版及通俗版同步 1、3、12.6、13、14、15、17：现状、模块、派生流程与 Mermaid、状态、使用方式、验证、影响范围。
README、CHANGELOG、题库说明、执行设计与 UE-3+ 合同同步；没有改写 ADR 的原设计决定。
本轮故事、ID、模型、种子只在显式测试或实验材料出现，通用诊断读取现行合同，未加入案例分支或隐藏默认值。
停止在诊断可复核：不自动评分、修改检索策略或采用候选；没有完整全量、L2–L4、新 72 题、联网安装、Wheel 或发布。
