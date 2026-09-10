# v0.10.0 发行决定

2026-09-10，用户授权先提交并发布 v0.10，再继续本机体验手册。

- 渠道：现有 GitHub 仓库的 Release，版本 `v0.10.0`；不上传 PyPI。
- 内容：S0–S4（含 S3-A、不含 S3-B）的宿主管理 Docker 沙箱、shell 与验证执行、可信插件 stdio 生命周期、原事件流执行回执，以及 TUI 运行环境下拉选择和手填。
- 唯一版本值来自 `src/traceh/version.py`；Session 13 / Context 12 不变，沙箱配置格式 2、Promotion 验证协议 2。
- 保留原事件日志、CAS 与各领域 Projection/Reader；不增加运行状态事实源。
- 用户明确不跑全量和 L2。本次使用定向回归、已有真实容器证据及发行包验证，提交消息带 `[skip ci]`，避免远程 push 自动触发全量。
- 不包含本机体验数据、真实密钥、未跟踪学习笔记、S3-B、MCP、自动拉镜像、网络白名单或跨平台支持承诺。

当前实测边界是 Windows + Docker Desktop + Linux 容器禁网。可信 Python 插件仍在宿主进程执行；文件写回是受限逐文件发布，不是全目录事务。宿主硬退出不自动恢复执行或清理停止容器。检索沿用此前 55/72 的已知结果，不将沙箱检查算作检索涨分。

验证详情见 [v0.10.0 限定验证](../validation-v0.10.0.md)。发布结果以 [GitHub v0.10.0](https://github.com/uboyme/traceHarness/releases/tag/v0.10.0) 为准。
