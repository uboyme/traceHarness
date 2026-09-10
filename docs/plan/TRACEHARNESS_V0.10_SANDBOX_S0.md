# v0.10 S0：沙箱后端与威胁边界启动计划

状态：S0 当前 Windows/Docker Desktop/Linux 后端真实实验已通过；S1/S2 已接通 shell、完成验证、Product 与文件写回。CLI/TUI 沙箱配置和只读观察已有定向检查，评估装配相邻回归、S3-A/S4 最终验收仍在进行；未配置的进程执行失败关闭，不宣称整个目标已完成。
依据：[总计划第 7 节](TRACEHARNESS_V1.0_MASTER_PLAN.md#7-v010sandbox-与隔离执行)。

## 目标与顺序

| 阶段 | 工作 | 完成证据 |
|---|---|---|
| S0 | 检查可用 OS/backend，实验文件、网络、凭据与进程树边界 | 真实允许/拒绝/取消测试，支持矩阵与 ADR |
| S1 | 冻结宿主拥有的执行请求和 receipt | owner/身份/预算/输出与原 EventStore 合同及反例 |
| S2 | 接入 shell、测试、构建与 Product 执行 | 同一主线中无隔离绕路，失败关闭和取消收敛 |
| S3-A | trusted adapter 管理隔离外部进程 | Generation/Lease/Drain 与进程资源共同收尾 |
| S4 | CLI/TUI 展示与平台真实验收 | 每个宣称支持的平台都有可复核证据 |

S3-B 通用 isolated Plugin 暂不纳入最小交付；当前 Python 对象贡献与宿主 setup 不会自动变成隔离协议。
MCP、自由 Workflow 和新检索策略不属于本阶段。

## S0 具体实验

1. 只读探测当前机器的容器/虚拟化运行时、版本和可用性；没有后端时明确记录缺失，不自动改系统配置。
2. 在临时测试目录准备允许文件和隔离区外哨兵。允许工作区读写；禁止外部读取/写入，覆盖 symlink/junction、父目录与设备路径。
3. 只注入实验变量与假凭据，验证宿主环境秘密不会继承。不要用真实 Key 测试隔离。
4. 验证默认禁网及显式允许语义。后端做不到细粒度 allowlist 时如实标为不支持，不能靠提示词约束。
5. 验证 CPU、内存、进程数量、时限和输出上限；受控子进程树的 timeout/cancel/重复取消/宿主退出后必须收敛。
6. 检查 worktree、临时目录、只读依赖与缓存的挂载或复制方式，避免把宿主凭据和 Git 管理目录一并暴露。
7. 根据真实结果选择最小支持后端并记录 ADR；后端缺失或合同不满足时 fail closed，不回退宿主 shell。

## 固定边界

- Sandbox 是 Host/Core 执行能力，复用现有 ToolRuntime、Policy、Budget、Effect 和 Session 身份。
- receipt 属于原账本；OS 句柄、容器和临时对象由执行 owner 持有，不另建事实源。
- Windows Job Object 只证明部分进程/资源控制，不能单独证明文件和网络隔离。
- S0 不修改 AgentLoop 职责，不开放不可信进程内 Plugin，不自动批准或迁移数据。
- 先跑相关真实隔离实验，不默认触发全量测试或 L2。

## 当前实际证据

2026-09-10：已有 Docker Desktop 的临时 IPC endpoint 修复后启动成功，原有镜像和数据保留。Linux Engine 29.2.1 / API 1.53、cgroup v2 可用；13 项真实实验通过，含内存 OOM、CPU 实际节流、进程树、宿主控制进程退出、文件和输出限制。见 [S0 验证](../validation-sandbox-s0.md)、[ADR-0065](../adr/0065-host-owned-sandbox-execution.md)和[执行记录](../deal/012-sandbox-execution.md)。

首版只实现 Linux 容器和禁网，不支持网络 allowlist 或自动拉取镜像。S1/S2 已复用原 owner/账本接通调用：真实 Product START 已到 awaiting_approval，shell 编辑和固定验证均有容器回执；原 Agent/Budget 身份已核对。Promotion 协议 2 绑定回执并拒绝旧 1，原始固定验证输出不持久化。中文配置与只读观察复用同一解析器/Reader，最新沙箱相关 59 项通过。评估装配的夹具修正后独立复跑通过；S3-A 有界 stdio、精确插件版本授权和原 Activation/Lease/Drain 已接通，相关 140 项定向检查通过。文件/TUI 插件授权与 S4 生产验收已完成，最终相关 25 文件 463 项通过；见 [S0–S4 验收](../validation-sandbox-s0-s4.md)。未运行全量或 L2。
