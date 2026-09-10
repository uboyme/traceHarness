# 017：UE-0 / UE-1 共享评估与 Product 切换

日期：2026-09-10。授权范围为 UE-0 与 UE-1；无 commit/push/tag/release 授权。
源码起点为 `53b7b6680882245ce093f4bc918f6e80ff3aeba8`，包版本仍为 0.10.0。

## 改动与原因

原 manifest、runner、report 直接拥有 Product 字段，无法在不复制调度器的情况下接第二类任务。
本次将公共输入、试次身份、调度、期限、关闭和报告外壳放在 evaluation；Product 的配置、评分和统计
迁入 evaluators/product*.py，并继续调用原 attempt / ProductChatHost。
原 Product/Workflow/Review/Promotion 成功条件与真实目标 ref 对账不变。

- UE-0 合同：[执行合同](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE0_CONTRACT.md)。
- 根协议 3，dataset 独立、材料摘要固定；旧根 1/2 明确拒绝，不留旧 Runner 别名。
- RunOptions / run-plan 将 current 变体、Product mode、重复次数分开；未知任务类型和未来比较输入拒绝。
- 材料、生产源码 ZIP、有效配置、全部试次在执行前冻结；每个试次前后拒绝漂移。
- 原 EventStore/CAS/Git 为业务事实源；公共 trials 保留失败、取消和未开始项，报告不缩减分母。
- 关闭失败停止后续调度，取消等待原资源；证据目录保留，JSON/Markdown 同源。
- 两个 shipped benchmark、CLI、F5 调用方、架构保护和两份配置示例同步。
- .gitattributes 保留冻结 JSON 与初始树的原始字节，防止跨平台 checkout 改写换行、使摘要失效。
  初始文件仅提交当前已验收的换行字节；忽略行尾差异后与 HEAD 无 diff。实际 Git 过滤前后 hash-object 相等。

## 验证证据

当前验证命令均为具名定向集合，下面按独立集合记录，不把重复复跑累计为新增测试。

1. `test_evaluation_architecture.py`、`test_evaluation_contracts.py`、`test_product_benchmark.py`、`test_product_architecture.py`：
   84 项通过，含实际 CLI 解析 run-plan 并进入公共 runner 的过期试次检查、输出重叠和缺失路径拒绝、真实 Git 字节过滤验证。
   程序化调用同时核对原 ModelRetryPolicy 的全部有效字段，不能用不同的 retryable_categories 冒充同一 plan。
2. Product single/multi 首轮真实 Git、Docker 固定验证与推广两个用例通过；随后执行完整 owner 定向集合。
3. owner 集合：`test_evaluation_lifecycle.py`、`test_product_benchmark_e2e.py`、
   `test_retrieval_evaluation.py`、`test_retrieval_evaluation_failures.py`。
   初轮 48 项中 46 通过、2 项旧取值断言失败（原因见下文）；F5 十一题实际 Product 11/11、
   检索质量 11/11、scope_violations=0，单项耗时 671.50 秒。
   修正断言和公共错误处理后，最终具名收口集合 13 项全部通过：生命周期 4 项、原 Product 重试 1 项、
   single/multi 2 项、F5 exact/lexical/zero-hit 3 项、F5 无判断与准备失败/取消 3 项。
   没有重复跑整个十一题网格；新增保护分别由定向正反及失败路径确认。
4. 相邻集合：CLI 环境配置、Product 配置、Workflow 执行及 Workflow/SQLite/Promotion/Workspace/Artifact 架构，86 项通过。
5. compileall、pytest collect-only（3786 项，仅收集）、修改范围 Ruff（33 个 Python 文件）、文档链接/章节/围栏/秘密/diff 检查通过。

反向验证：在独立测试进程暂时移除“run-plan 的显式 null 不受环境变量覆盖”保护。
公开 CLI 配置路径把 base_url 改为测试环境中的 `https://unused.invalid`，新增断言按预期失败；
恢复后同一测试通过。未修改运行中的生产源码，不接触真实连接或密钥。
报告保存同样作反向验证：移除原错误保留后，报告写失败只抛 OSError、丢掉 TimeoutError；
恢复后两者进入同一个异常组。配置目录缺失、输出重叠与未启动分母均有定向检查。

迁移前后 AST 核对：Product metrics 的原声明完全一致；Product report 除导入位置和公共 markdown 适配外，
原 DTO、成功条件、指标与统计声明完全一致。公共层没有第二套 Product 成功规则。

初轮失败已分别定位：取消测试引用了夹具不存在的 README，重试测试仍从旧 JSON 根读取 Product 字段；
归档最终与材料原字节比较，保留 Windows CRLF，不在测试中假设 LF。
Promotion 架构白名单需精确迁移到新 Product owner 文件。相邻 Workflow 即使补齐测试沙箱环境仍失败，
在独立解包的干净 HEAD（确认 import 来源为隔离 src）上精确复现同两条失败。
根因是 workflow_fixtures 未传 Sandbox service，且命令还用宿主解释器路径。改为已有 promotion_service
测试装配与容器中的 python 后，上述 86 项通过；未改变生产 Workflow、执行权限或成功条件。

真实执行仅指本机 Git 与现有 Docker 镜像中的固定验证、实际 Session/Store/CAS/Promotion；
模型使用确定性测试 Provider。没有真实模型效果测试，不产生新的 72 题成绩。

## 文档同步和边界

正式版先同步，再按正式版同步通俗版：1 当前状态、3 模块目录、12.5/12.6 评估流程与 Mermaid、
13 CLI 配置、14 尚未实现的边界、15 验证、17 影响矩阵；正式版 20.30 保留历史决定并注明当前入口与文件位置。
README、benchmark README、总体设计、ADR 实施状态和 CHANGELOG Unreleased 同步。

未运行全量、L2–L4、包构建/安装、真实 API；未发布提交。
未实现 UE-2 RetrievalEpisodeEvaluator、UE-3 比较/隔离、人工 review/assess 或 AO 优化。
不声明源码 ZIP 是完整依赖锁定，远端模型 revision 与实际代理路径不可核对时明确未知。
宿主硬退出、跨 Store 全局 token 预算、恶意 Python 隔离仍不在本阶段承诺内。
原 55/72 是历史合成成绩，不当作本次结果。用户已有的学习笔记和 claude-recmd 内容保持不动。
