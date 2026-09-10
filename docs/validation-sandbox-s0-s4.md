# Sandbox S0–S4 定向验收

日期：2026-09-10。范围为用户授权的 S0、S1、S2、S3-A、S4；S3-B、MCP 协议、自由 Workflow、
不可信进程内插件隔离不在范围。本轮没有运行全量测试、L2、Wheel/安装、真实模型 API，也没有提交或发布。

## 验收范围与证据

| 要求 | 当前实现与可复核证据 |
|---|---|
| S0：后端与威胁模型 | [ADR-0065](adr/0065-host-owned-sandbox-execution.md)、[S0 实验](validation-sandbox-s0.md)、[13 案例记录](eval/sandbox-s0/probes.json)；生产执行器另有下述真实验收，不用原型替代 |
| 文件系统、链接与路径 | `test_sandbox_contract.py` 的父路径、设备别名、hardlink、Windows junction；`test_sandbox_publication.py` 的授权回写、原快照冲突及部分失败；生产 `test_sandbox_acceptance.py` 的只读根与受保护输入 |
| 凭据与环境 | 生产验收显式放入假宿主凭据和假 .env，客体确认未继承/未复制；UID 65532，无 Docker socket/宿主工作区挂载。授权范围中的普通文件仍可能含秘密，过滤名称不等于内容审计 |
| 网络 | `network=none`；生产客体外连实际失败。其他网络模式明确拒绝，没有 allowlist、DNS 代理或自动下载 |
| CPU、内存、进程 | 生产验收读取真实 cgroup 的 cpu.max、节流计数、pids.max；fork 达限触发 EAGAIN；大内存分配产生 oom_kill。资源不是仅写在配置里的字段 |
| 时间、输出与后代 | Docker/Verifier/stdio 定向检查覆盖内部期限、输出洪水、真实启动后的重复取消、后台后代和解释器关闭。父命令终态触发收尾，后代持有管道不再拖到超时 |
| 挂载与缓存 | 只读控制文件＋限额客体 tmpfs，复制授权普通文件/目录；不挂载 HOME、Git 管理目录、包缓存、Docker socket，不自动拉取镜像 |
| S1：不可变执行与身份 | `api/sandbox.py`；原 owner、Session/Turn/Step/Tool/Agent/Budget 引用适用时冻结；argv/env 以摘要进入回执链。后端版本、容器身份、启动/结束时间、策略、结果和 CAS 引用可核对 |
| 唯一事实源 | `sandbox/ledger.py`、`reader.py` 验证原 stream 身份/摘要/顺序；输入与输出使用原 CAS。没有新数据库、第二运行状态或消息事实源；临时连接和句柄不决定业务事实 |
| 取消、失败与未知 | 原 scope 等待 worker、清理和落盘收敛；重复取消不提前返回，清理异常保留。缺失后端失败关闭；清理无法确认时记录 unknown-convergence，不自动重试 |
| S2：Tool/Verifier/Product | `test_sandbox_integration.py` 验证真实 shell 编辑、完成检查及原预算身份；`test_sandbox_product.py` 走真实 Git/Product START 到 awaiting_approval；没有自动批准或推广 |
| Review 与证据隐私 | `test_sandbox_promotion.py` 核对原 Review owner、策略与回执，错误归属/过期策略拒绝；固定验证只持久化输出摘要和字节数。Promotion 协议 2 拒绝旧 1 |
| S3-A：本地 stdio server | `test_sandbox_plugins.py` 通过真实 Runtime/PluginManager/setup 和 Agent Tool 调用，服务器输入输出走同一执行器；原 Effect 和请求快照可重放 |
| 插件身份与生命周期 | 宿主精确授权 ID/version、工作区、stdio 额度和激活尝试次数；先注册 Activation cleanup 再启动。旧 Lease 可继续收发，释放后 Drain；setup 失败/取消、重复 shutdown、清理错误都有真实用例 |
| 传输有界与不确定写入 | `test_sandbox_stdio.py` 检查中文双向传输、每帧/累计输入拒绝、尾部读取、期限和已实际消费部分输入后的取消；关闭连接后不能重发。只实现字节传输，不实现业务协议或隐式重启 |
| S4：配置体验 | 格式 2 的唯一解析器；中文表单可增删插件授权、填写身份/绝对路径/额度、保存或取消。实际 CLI 从文件装配插件并调用服务器，最后核对原 SQLite 回执；配置本身不执行命令或启用插件 |
| S4：只读观察 | `/sandbox` 同时读当前 Session、Effect 和宿主应用级激活流；显示实际后端、权限、额度、终态、时间和回执。完成验证与工具的真实两份回执均可见；不展示命令或输出正文 |
| 宿主 crash | 生产验收直接杀掉持有活 stdio 的宿主 Python 进程，Docker wait/inspect 确认客体期限后 Running=false、Pid=0；原 SQLite 仍只有 request，Reader 保持 outcome=None。测试显式核对标签后清理自己容器，未伪装成生产自动恢复 |
| 平台承诺 | 仅下表组合有真实验证；未测试的平台或更早版本不宣称已支持 |

## 已验证平台与依赖

| 层 | 已验证值／支持边界 |
|---|---|
| 宿主 | Windows，项目要求 Python 3.12+；本轮使用已有本地 Python 环境 |
| Docker | Desktop 4.65.0，Linux Engine 29.2.1 / API 1.53 |
| 客体内核 | WSL2 Linux 6.6.87.2，cgroup v2 |
| 镜像 | 本机已有官方 Python 3.12.13 slim-bookworm，按 sha256 身份显式选择；镜像/Python 控制运行时属于宿主选择的可信基础，固定摘要不是供应链认证；不自动拉取 |
| 最低承诺 | 上述是最小已实测组合，不承诺更早版本。运行时另检查 Linux、cgroup v2、CPU/内存/进程控制与镜像身份；不满足则失败关闭 |
| 其他宿主／后端 | 本轮未验证，不发布支持承诺；Windows Job Object、远程后端、网络 allowlist 均未实现 |

## 检查记录与根因修复

最终合并定向门禁为 **25 个受影响/相邻测试文件，463 passed，0 skipped，226.741 秒**。
随后 `/sandbox` 完成验证流的修复另跑 **4 passed**；部分输入写入取消另有独立实际进程检查。
各组有重叠，不能相加冒充全量；原型、历史阶段检查和最终生产检查分别记账。
具体计数见 [验收摘要](eval/sandbox-s0/final-checks.json)。

本轮实际发现并修复的两个收尾问题：父命令退出却因后代输出句柄被误报超时；观察器漏读完成验证所在
Session 流。两者均先用公开生产路径失败复现，再修 owner。版本授权、Review owner/策略、hardlink、
取消清理原因和父命令结束保护有移除后按预期失败的反向证据；不以未启动进程的空验证作证明。

完整源码/测试编译、全库 collect-only、修改范围 Ruff、diff 检查、正式/通俗文档章节对应、相对链接及
Mermaid 代码块由最终摘要记录。这里只收集全库测试，没有执行全量。

## 保留的明确边界

- 可信 Python 插件仍与宿主同权限；SDK 不提供扩权入口，不等于能隔离恶意进程内 Python。
- Core 文件工具继续用原工作区/权限边界；进程沙箱的文件范围不冒充所有模型工具的统一权限。
- 工作区写回逐文件执行，记录部分完成；不承诺跨文件事务或防御另一个同用户进程的任意并发修改。
- 宿主硬退出后内部期限限制客体执行，但未实现自动孤儿清扫、冷恢复或自动补写终态。
- eval 拒绝非空应用级服务器授权，防止共享工作区混入独立 attempt；普通命令沙箱评估仍走原主线。
- 本工作树为未发布实现；未跑的全量、L2、Wheel/安装和发布检查不得写成通过。
