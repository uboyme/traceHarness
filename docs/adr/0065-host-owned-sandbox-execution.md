# ADR-0065：宿主拥有的隔离执行与原账本证据

日期：2026-09-10。状态：已采用；S0–S4（含 S3-A、不含 S3-B）限定验收完成，尚未发布。

## 问题

现有 shell 和固定验证命令直接在宿主启动进程。Policy、环境变量过滤和 Git worktree
不能隔离文件系统、网络或逃逸孙进程。不能通过增加另一套运行状态、插件权限或执行循环解决这个问题。

## 决定

1. Sandbox 位于 Host/Core。领域 owner 先完成 Policy、Budget 和身份准入，再生成不可变执行请求。
   AgentLoop 保持编排职责，模型和 Plugin 不获得 Docker 参数、挂载、镜像选择或权限升级接口。
2. 首个后端使用 Linux Docker 容器、cgroup v2、默认 seccomp、只读根文件系统、禁网、capability 收紧。
   显式配置 Docker context 和本地 SHA-256 镜像身份；不自动拉取，不用标签猜镜像，不回退宿主执行。
   宿主选择提供 Python 控制运行时的可信镜像；固定摘要用于身份绑定，不等于供应链认证，镜像运行时属于可信基础。
   首版不支持域名/IP allowlist；请求其他网络模式明确拒绝。
3. 不挂载工作区、HOME、Git 管理目录、包缓存或 Docker socket。宿主只复制授权且有界的普通文件，
   排除受保护名称及明确排除的存储目录；拒绝 symlink/junction/reparse、hardlink、设备路径和路径别名。
   输入暂存为只读控制文件，工作目录与临时目录使用限额 tmpfs。任意工作区文件不自动等于无秘密；
   工作区读取范围仍是宿主授予的能力，保护名称过滤不能替代正确选择范围。
4. 可信 PID 1 监督进程保留 SETUID/SETGID/KILL/CHOWN，工作负载降到另一 UID，清空继承环境。
   超时由容器内部监督进程控制，宿主退出后仍生效；工作负载无权暂停或杀死监督进程。
   结束时杀死并回收整个 PID namespace 的工作负载，再收集有界结果。不是宣称整个容器都以非 root 运行。
5. `SandboxRequest` 绑定 owner、Session/Turn/Step/Tool（适用时）、原 Budget reservation、工作区、
   argv/cwd、环境摘要、镜像与资源限制。Budget 字段仅引用原准入结果，不创建新预算账本。
6. `sandbox/request` 和 `sandbox/outcome` 写入**原 owner 的 EventStore stream**，原 CAS 保存输入/输出字节。
   request 先落盘再启动；同一执行身份只准入一次；outcome 核对原 request digest 与 owner。
   不新建数据库，不用容器列表、临时文件或内存字段替代事实。容器是临时执行资源，不是业务状态。
7. 取消拥有执行 worker 和最终落盘任务；重复取消不得提前放走资源。无法证明容器已清理时记录
   `unknown-convergence` 并报错，不写成已取消成功。宿主硬退出后进程期限仍有效，但停止后的容器、
   临时文件和缺失 outcome 不等于已经自动完成冷恢复；恢复展示及对账属于后续接入验收。
8. S2 在原 Tool/Verifier/Product owner 接入同一个执行器；宿主工作区写回独立核对快照、权限与
   当前状态，并记录 publication 与部分完成操作，不承诺目录级事务。验证执行不发布工作区改动。
   固定 Review 只持久化输出摘要/字节数及原执行回执，通过 Promotion 协议 2 绑定 owner 与有效策略；
   旧协议 1 明确拒绝，不迁移或猜测。request 的 argv 以摘要记录，避免复制固定验证命令中的敏感文字。
9. S3-A 只处理可信 adapter 的外部进程及 Generation/Lease/Drain 收敛。可信 Python 插件本身仍与宿主
   同进程同权限；不能声称防住恶意可信插件。S3-B、MCP、自由 Workflow 不在本次范围，isolated 继续拒绝。
10. S3-A 的本地服务连接采用有界 stdio 字节传输，输入总量和单帧大小由宿主显式授予，输出继续使用
    原执行输出限额。可信 PID 1 持有仅 root 可访问的控制 socket；Docker exec 只运行固定的核心字节
    转发程序，不接受插件提供的脚本、容器身份或提权选项。业务子进程仍为 UID 65532。传输取消或
    不确定写入不能自动重试；必须收敛对应执行。Activation 拥有连接，Generation Lease 决定何时可
    Drain，原 EventStore/CAS 保留执行证据。此项是 S3-A 的新增设计决定；程序化主线已接通并完成
    真实验证，格式 2 文件/中文授权表单与最终生产验收已完成。

S3-A 装配把同一个 SandboxExecutionService 提前到原 Runtime prepare 阶段，再向原 Builder/Manager
传递绑定工厂。Manager 不拥有第二执行器，只在 setup Context 提供受限 open_process；Activation 仍
拥有 rollback/dispose，Generation 仍拥有 Lease/Drain。相应源码指纹在逐项核对这条装配变化后更新，
未解除 Product 架构保护，也未改变 AgentLoop、Supervisor 或预算规则。

## 支持与验证边界

真实验证机器是 Windows + Docker Desktop 4.65.0，Linux Engine 29.2.1 / API 1.53，WSL2 Linux
6.6.87.2，cgroup v2。这是一组已测组合，不是对任意 Docker 版本或其他宿主 OS 的验证声明。
后端还会检查 Linux、资源控制能力、精确镜像及拒绝镜像自带匿名 volume。

[S0 真实记录](../validation-sandbox-s0.md)覆盖允许读写、禁网、环境过滤、只读根、进程数量、输出、
内存、CPU 节流、后台孙进程、宿主控制进程退出、取消及文件数量/字节限制。
程序化 shell、完成验证、Product START、固定 Review、插件 stdio/Lease/Drain 与 GUI 均有真实定向检查；
完整范围和保留边界见 [S0–S4 验收](../validation-sandbox-s0-s4.md)。未运行全量或 L2，不等于完成发布门禁。

依据：[Docker 运行选项](https://docs.docker.com/engine/containers/run/)、
[资源限制](https://docs.docker.com/engine/containers/resource_constraints/)、
[Docker 安全边界](https://docs.docker.com/engine/security/)。
