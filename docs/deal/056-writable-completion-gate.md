# WC-4：可写完成门禁与有界返工

2026-09-13。用户要求继续修改实现。记录 054/055 的真实失败保留，本次没有再次调用模型，也没有修改题目、参考解、固定评分或旧证据。

## 原因与修改

先前只有提示和当前文件观察；主方可以看到违规文件仍宣称完成，还可以用普通编辑复制助手代码，绕过显式整合。Product 的可写主方未接 Runtime 已有的 CompletionVerifier，因此固定检查在主方退出后才失败，原有失败反馈与返工机制没有生效。

- Product/completion 绑定确认 Profile 的冻结 VerificationPlan，复用原 HostVerificationRunner、Sandbox、Workspace 和 Effect/Artifact Reader。
- 仅可写 multi 的 REVIEW 在 StepViewSelection 中携带 CompletionVerifier，接入原 ActiveComposition；SCOUT/PLAN/EXECUTE 不伪造检查通过。AgentLoop 未修改，仍拥有原 verification/result、失败计数、反馈和停止。
- 当前 Turn 的 Effect intent 关联 outcome，核对唯一完成交接、原 CAS、直属 owner 和精确目标请求，再要求对应 integrate_child_patch 的 applied 回执。Outcome 本身没有 Turn 字段，不能直接按它筛选；实现阶段发现并修正了这一关联错误。
- 同次完成检查也在原 Workspace 锁内执行冻结检查，缺少整合不会遮蔽其他失败，客体改动不回写；前后候选不一致也失败。固定输出不保存，反馈只含状态、退出码、摘要和原回执引用，不暴露验证器源码或原始输出。
- 原默认一次返工机会、至多两次完成检查，仍受原调用、Step、token 和时间上限约束；失败不进入审批。后续正式 Artifact Review 和人工 Approval/Promotion 继续保留。

架构依赖守卫按具体文件/符号更新，未放宽成整个目录许可；同时补齐已有只读 Evaluation product_review 对原 Promotion Ledger 的声明。固定 runner 的 Session 流身份使用原 SessionService，未新建流。

## 验证

四条公开 Product 功能路径通过：多余文件、顶层输入错误、两者组合，均先被冻结检查拒绝，在下一请求收到失败反馈，再用原工具修复并复验；持续不修复在第二次检查失败后停止。每个用例都核对原 Sandbox 回执、请求重放及没有人工批准/推广。生产没有测试夹具路径或输入类型默认值。

功能闭环 4 passed；主线交接/整合/取消/Evaluation 19 passed；架构/请求/装配/Registry/runner owner 拒绝 71 passed；冻结协议和相邻架构 87 passed；相邻固定 Sandbox、Runtime 反馈及收尾观察 16 passed。另有原 Runtime 客体重复取消 1 passed。集合分开报告，不相加充当全量。

取消用例通过原 sandbox/request 信号，再观察真实 UID 65532 客体进程，之后重复取消聊天；原 sandbox/outcome 确认 cancelled/converged，没有 verification/result 或批准。重新打开同一 Product 后执行原显式 cancel，任务 cancelled、整树账户 closed。早期测试观察器未形成有效启动证据、以及把聊天中断错误等同于账户关闭的断言，均已修正；没有改生产时限、取消语义或隐藏失败。

反向验证在独立进程中进行，未改源码：移除整合拒绝后，未整合的主方错误进入 awaiting_approval；移除 REVIEW 的 verifier 后，错误输入得不到失败反馈和修复，最终 Workflow 失败。两次均由公开 Product 行为断言失败，真实工具/固定检查已经执行，不是夹具导入错误。恢复后重跑缺少整合与输入修复两个关键用例，2 passed（70.92 秒），结果记录在 `.traceh/wc4-completion-restored.txt`。

compileall、修改范围 Ruff、collect-only 4196、git diff --check、文档相对链接/代码块/0–20 章节对应和秘密模式检查通过；两轮原始真实数据库 SHA-256 均保持不变。原始定向输出位于 `.traceh/wc4-completion-*.txt`。

## 限制与交付

冻结检查只能覆盖它显式声明的条件；自然语言里的全部语义、模型报告真实性及忽略文件范围不会因此自动获得证明。整合回执证明真实整合发生过，后来修改仍由当前检查和正式 Review 判断。

聊天中断与显式取消任务是原有不同操作：前者收敛执行、保留账户和未提交状态，后者通过原控制面释放资源、关闭整树账户。本修复不把前者偷偷改成后者。

两份上下文同步 1、10、14.3.6、15、16、Workspace 与固定验证相关主题，补充当前流程图；修正 D2 旧 native verifier/输出/后代进程说明。精简入口、读取导航和 WC 计划/交接同步当前实现。决定见 [ADR-0073](../adr/0073-writable-completion-verification.md)。

没有全量、L2–L4、Wheel、安装、新真实模型、提交、推送或发版。WC-2/3 完成；本修复不能冒充 WC-4 新的真实完整验收通过。
