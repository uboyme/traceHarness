# Release Stop C 独立审查范围冻结

- Stage：C5；C1/C2 已按冻结真实门槛验收，C3 已评估并按门槛不接入，C4 已完成限定调查。
- 基准提交：`1c72771`，即 F1–F3 与 Stop A/B 收口；审查当前工作区 F4/F5、检索/导航整改及 C1–C4。
- 本轮是限定 Stop C，不是 v0.9 发布候选认证；禁止全量、L2–L4、Wheel/安装、commit/push/tag/release。
- 独立审查默认离线，不加载 `.env`，不调用真实 Provider，不安装包。C1/C2/C4 的原真实证据可只读核查。

## 1. 具体文件与事实源

审查以当前源码、测试、协议为先，随后是两份上下文、ADR 和计划。`c5-review-frozen.json` 在独立审查
开始前保存 HEAD、修改范围文件 SHA-256 以及全部生产模块摘要。包括本功能已有未提交改动，不能将其
当成无关内容丢掉。以下用户已有内容明确排除：`.pytest-tmp-codex/`、`docs/claude-recmd/` 和
`docs/TraceHarness_Py_完整项目学习笔记.md`；不读取秘密文件或认证值。

当前唯一协议：Session 9、Context 8、context-json-v8、f5-context-policy-v5、来源 receipt 2、
披露 receipt 2、History page policy v2、SQLite/M3 2、lexical tokenizer v3/ranker v2。
旧 Session 1–8 明确拒绝，无兼容迁移、双读或自动删除。

## 2. 当前公开生产入口

1. 原 Runtime create/run/resume、SessionService append/read/replay、Composition Lease、Request
   compose/admission/dispatch/retry；唯一 Context 后置于完整 Surface，模型视图由原 Context 派生。
2. Skill host selection/rebuild 和 request_skill_reference；Memory host declare/approve/revoke/
   supersede/rebuild、项目绑定与 request_workspace_memory；当前 Session History 原页与 request_history_page。
3. Line/TUI 的共享 ChatGovernance、显式 context config、Plugin Manifest 审阅与原加载/释放；
   Product 原角色绑定、Session-specific Memory 索引与资源生命周期。
4. 同 Store 的派生 FTS、资格过滤后的 exact/BM25/RRF、覆盖准入/统一预算；原 ProductBenchmarkRunner
   的 seeding、冻结 judgments、实际 Context 评分与失败分母。开发诊断必须准确声明自身边界。

## 3. 已声明的信任与非目标

事件日志是唯一事实源，View/Context/index/模型导航都是派生结果；不新增可变 `runtime.state` 或
messages 权威。Skill 文本、Memory 文本、历史内容与模型参数不授予 host 权限；原来源资格、Scope、
当前 Lease、CAS、目标 Step、连续准入、取消收敛和预算继续由原 owner 保证。

Plugin 仍是显式 trusted in-process 代码，不凭任意 Python 对象无限扩大威胁模型。没有向量生产入口、
外部向量服务、跨 Session 原始 History 检索、新工作流、MCP 平台、多 Agent 产品改造或发布操作。
检索偶然词面重叠和模型不总按格式回答属于已声明能力边界；具体违反当前权限/来源合同仍须报告。

## 4. Finding 与停止规则

严格执行 AGENTS.md §8。P0/P1 必须同时具有公开生产路径、可重复触发、实际错误、被破坏的当前合同、
真实影响及现有测试漏检原因。只查看私有字段、测试没进入目标主线、夹具导入/TypeError、未来功能或
代码偏好不能作为阻断。P2 也需可观察生产影响，默认不阻断；文档措辞与建议另列。

审查者只读源码，必要的最小复现在各自临时目录中进行，不编辑共享生产文件。提出 Finding 后由主代理
核查和修复，再独立确认。当前范围 P0/P1 清零后停止扩展理论输入，进入具名定向确认；没有全量授权。

## 5. 独立分工与已有证据

- Context/来源证明：请求位置、正文连续准入、三类来源权限、历史时效、协议、重试/重放与 Inspector。
- 治理/生命周期：Line/TUI/config、Plugin review/activation、Product 绑定与 Memory 索引准备、取消与失败收敛。
- 检索/验证：shared rank/index、预算/精度、原 evaluator、真实与本地诊断的判定和证据边界。

C1、C2、C3、C4 的完整正反例、失败与未运行门禁分别见
[C1](validation-v0.9-stop-c-c1.md)、[C2](validation-v0.9-stop-c-c2.md)、
[C3](validation-v0.9-stop-c-c3.md)、[C4](validation-v0.9-stop-c-c4.md)。
旧汇总不是新的执行证据；独立报告必须列明实际检查和未运行门禁。
