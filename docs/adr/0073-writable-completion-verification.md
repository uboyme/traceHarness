# ADR-0073：可写主方的有界完成验证

状态：接受。日期：2026-09-13。

记录 054/055 证明：把交付约束写进提示、展示实际文件和原 Patch，不会强制模型遵守。第二轮主方还绕过显式整合工具手工复制代码。用户要求继续修改实现。

## 决定

启用 patch_author 的 Product multi 主方，在 REVIEW 阶段绑定 WritableCompletionVerifier。先从当前 Turn 的原 Effect intent/outcome 读取唯一完成交接，重新核对原 Artifact/CAS、直属 owner 和目标 Workspace 请求，要求对应 integrate_child_patch 的 applied 回执。同次完成检查还执行确认 Profile 中已冻结的 VerificationPlan，缺少整合与固定检查失败一起反馈，复用 HostVerificationRunner 和原 SandboxExecutionService；不从模型自然语言猜测文件白名单或业务断言。

通用 StepViewPolicy.select 返回 StepViewSelection，其中 RequestView 继续按现有格式冻结，额外可携带当前 Step 的宿主 CompletionVerifier 能力。freeze_step_view 将能力接入原 ActiveComposition；已有 verifier 冲突时拒绝。SCOUT/PLAN/EXECUTE 不安装这项检查，也不伪造“跳过但通过”的结果。AgentLoop 无修改，仍拥有 verification/result、失败计数、反馈消息和原 DefaultContinuationRuntime；默认最多一次失败后的修复，仍受原 Step、token、调用和墙钟上限限制。

固定 runner 接受绑定 Session/Turn/Step 的 verification owner，回执落原 Session 流；原 Promotion owner/Effect 流保留。通过共享 SessionService 计算流身份。命令、环境、超时和输出上限来自冻结计划，原始固定输出继续不保留；反馈包含命令 id、状态、退出码、摘要及执行引用，不向模型公开验证器源码或原始输出。

Product 在原 Workspace 锁内执行隔离检查，并比较前后候选观察；沙箱不回写验证产生的文件。取消须先由原 Sandbox/Workspace/Supervisor 收敛，不制造验证通过事件。整合回执只证明发生过精确整合，后来修改的正确性由当前检查与后续正式 Review 判断。

## 边界

本检查不生成 Artifact Review、Approval 或 Promotion，不代替 Workflow 后续对最终不可变 Patch 的固定 Review。没有第二事实源、任务队列、重试状态机或模型权限。Session 14、RequestView format 1 和 Product 持久协议不变，不保留旧的内部 select 返回形态。

固定检查只能覆盖显式声明的条件；候选文件清单只涵盖 Git 可见改动，不能推导未声明的完整需求或整个磁盘状态。本轮不加入 single/只读 multi 的新完成政策，不改历史评分或失败证据；没有新的真实模型轮、全量/L2、提交或发行。
