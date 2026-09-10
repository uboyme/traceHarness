# 统一 Evaluation 设计调研

日期：2026-09-10。源码基线：`53b7b6680882245ce093f4bc918f6e80ff3aeba8`。
用户授权调研后落盘可执行设计；本轮不实施代码、不运行模型、不提交或发布。

## 核对范围

- 当前 evaluation manifest、runner、attempt、metrics、report、retrieval 与 CLI eval 装配。
- 原 Product/Workflow/Review/Promotion 的成功条件、失败收敛及现有相关测试。
- tests/live_active_retrieval 的 manifest、fixture、运行、暂定评分与审阅实际请求证据逻辑。
- 旧 72 条、grid-06 与直连补测成绩的来源口径。
- 原 PluginContext typed service、evolution L3/L4 与 Sandbox 边界。
- 两份项目上下文及 ADR-0033、0017、0018、0065。

## 决定及交付

保留一个 traceh eval，将 ProductTask 和 RetrievalEpisode 接入公共实验/结果/比较合同。
领域成功条件仍由各 evaluator 读取原事实判断；原 F5 retrieval 指标与独立旅程评分明确区分。
优化策略后置在 evolution + 窄插件服务，不给策略模型评分、预算或采用权限。

见[设计全文](../plan/TRACEHARNESS_UNIFIED_EVALUATION_AND_OPTIMIZATION_DESIGN.md)与[ADR-0066](../adr/0066-shared-evaluation-and-bounded-optimization.md)。
实现计划为 UE-0～UE-4，其后 AO-0～AO-2 另行授权。本轮不建立这些拟议模块。

两份上下文同步第 1、3、12、14、15 节：当前/计划状态、目录职责、共享评估设计、待建服务与验证边界。
另修正当前地图内 evaluation 根协议的过时 schema-1 描述，并使通俗版的历史 NO-GO 与已授权发行状态一致。
历史 ADR 不因当前设计改写。

## 本轮验证

文档限定检查通过：5 份新增/修改文档共 603 处相对链接均存在，两份上下文的 0–20 章编号一致；
代码围栏闭合，新增设计图的节点/连线结构及依赖方向完成检查。图未调用外部渲染器，不把结构核对描述成渲染测试。
4 组列出的现有定向 pytest 命令对应文件均存在，仅核对路径，未执行这些测试。
新文本通过 UTF-8、常见密钥格式和尾随空白检查；git diff --check 通过，无源码/测试/配置的修改。
通俗版原有 U+FFFD 字符是解释解码丢失的刻意示例，与 HEAD 相同，不属于本轮乱码或待修缺陷。
不把研究阅读、文档检查或历史测试数量当成本轮实现验收。
本轮未执行全量、L2–L4、真实 Provider、Docker、Wheel/安装或发布操作。
未读取真实 .env/Key，未修改源码/测试/配置；保留用户未跟踪学习笔记与 docs/claude-recmd。
