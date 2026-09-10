# Sandbox S0 真实后端实验

日期：2026-09-10。范围：S0 后端可行性及 S1 核心执行器定向检查，不是 S2–S4 发布验收。

## 已验证事实

| 项目 | 实际结果 |
|---|---|
| 允许工作区输入/输出 | 普通输入可读，中文候选文件可返回；未直接写回宿主 |
| 文件系统 | 根文件系统写入失败；无宿主 HOME、Git 或 Docker socket 挂载 |
| 环境 | 假宿主 Token 不继承；工作负载 UID 为 65532 |
| 网络 | 禁网容器的外连被拒绝；其他网络模式不支持 |
| 监督进程 | 工作负载暂停 PID 1 被 PermissionError 拒绝 |
| 进程数量 | 限制 32，fork 在 30 个子进程后触发 EAGAIN |
| 输出 | 超出每流 4096 字节停止，保留量恰为 4096 |
| 内存 | 128 MiB 限额下申请 256 MiB，被 OOM 杀死；memory.events 有 oom_kill=1 |
| CPU | cpu.max 为 50000/100000，cpu.stat 确认实际 throttling |
| 非法输出 | 指向控制文件的 symlink 被拒绝，不返回文件 |
| 后台孙进程 | 父命令完成后回收独立 session 的忙循环孙进程 |
| 宿主控制进程退出 | start CLI 退出后，无常驻宿主监督；2 秒客体期限仍终止工作负载 |
| 显式取消 | 先观察 UID 65532 子进程实际启动，再 kill；退出码 137 |
| 文件预算 | 文件字节总量和文件数量超限都拒绝 |

13 个案例全部通过；每例结束验证 Running=false、Pid=0，然后删除该例精确命名容器。
结果摘要见 [probes.json](eval/sandbox-s0/probes.json)。数值均为实验配置，不是系统隐藏默认值。

## 可复现入口

[实验运行器](../scripts/sandbox_s0/probe.py)要求明确指定 `--context`、本地镜像 SHA-256 `--image`
以及新的 `--output` 目录。使用的镜像为官方 Python 3.12.13 slim-bookworm，实验身份记录在结果中。
脚本不会安装镜像、启动服务、跑项目全量测试或 L2。

核心 API 的定向测试为 `tests/test_sandbox_contract.py`；真实核心执行器检查为
`tests/test_sandbox_docker.py`，仅在明确设置 `TRACEH_SANDBOX_TEST_IMAGE` 和
`TRACEH_SANDBOX_TEST_CONTEXT` 时运行，否则跳过。真实检查与 S0 原型分别记账，不互相冒充。
核心检查覆盖原 CAS、原 EventStore writer、重复执行身份拒绝、缺失后端、内部期限和重复取消。
当前定向结果为 47 项核心/真实执行/EventStore 检查通过，加 18 项相邻 CAS/Catalog 检查通过；
其中清理失败反例先真实执行命令再拒绝 rm，核对原账本保存 unknown-convergence。
3669 项仅 collect-only；compileall、修改范围 Ruff、相对链接、两份上下文章节与代码块检查通过。
结果摘要见 [core-checks.json](eval/sandbox-s0/core-checks.json)。

硬链接拒绝做了反向验证：临时移除生产快照层的 hardlink 保护，原公开快照路径测试因
`DID NOT RAISE` 按预期失败；恢复原文件字节后继续正常验证。

## 环境修复与未完成边界

最初 Docker Desktop 启动失败，原因是本地临时 IPC endpoint 无法移除。只备份重命名了相关
临时 endpoint 目录并重新启动 Desktop，没有恢复出厂、清空镜像、修改 VHD、删除用户容器或读取凭据。
已有镜像保留，修复后的 Engine 和实际能力探测成功。

这是当前 Windows/Docker Desktop/Linux engine 组合的证据；其他平台未实测。
没有运行全量、L2、联网包安装或真实模型 API。上面记录的是 S0/S1 检查时点；后续 owner、Tool/Product/
Verifier、S3-A 和 S4 的当前实现及验收见 [S0–S4 验收](validation-sandbox-s0-s4.md)，不把本文件的原型证据冒充生产验证。
