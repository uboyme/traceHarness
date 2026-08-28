# TraceHarness Py v0.7.1 验证记录

验证日期：2026-08-28

状态：全部发布门禁通过；以 annotated tag `v0.7.1` 发布。

本记录描述 `v0.7.1` 的窄维护发布。它补充而不改写
[`validation-v0.7.0.md`](validation-v0.7.0.md)：v0.7.0 的 ProductTask 真实模型网格、
F5 安全审查和历史资产仍是当时的发布快照。

## 1. 发布范围

v0.7.1 只修复三个已通过公开生产路径复现的边界：

- Product Chat 中，模型的 `confirm_product_task` 只能请求宿主提示；终端用户必须对
  屏幕上的精确 task 输入固定 `START` 才创建 ProductTask 和资源；
- AgentLoop 取消由一个 owned finalizer 按 Attempt → Step → Turn 写完 durable 终态，
  重复取消不能让公开调用提前返回，独立收尾失败也不会被吞；
- L4 在 `-I -S` 下检查目标 venv 时显式使用标准 `venv` sysconfig scheme，并要求
  `purelib`/`platlib` 留在目标前缀内。

最终门禁还发现两个独立示例插件的 Wheel dependency 与运行时 Manifest 仍排斥
0.7 核心。拥有元数据的插件各自修复并升为补丁版本：Plugin Creator `0.2.1`
支持 `traceharness-py>=0.6,<0.8`，Python Quality `0.2.1` 支持
`traceharness-py>=0.5,<0.8`；Creator 的当前候选模板只生成 `>=0.7,<0.8`。

本版没有实现 v0.8/v0.9 的 TUI、SQLite、Memory、常驻 Skill、Provider
retry/fallback 或 OS sandbox，也没有改变 ProductTask/Workflow schema、人工 Approval
或 bare target Promotion 权限。

## 2. 审查、反例与自动化门禁

冻结范围复审结果为 `P0=0 / P1=0`。三个原始修复都完成了逐项反向验证：

- 去掉宿主 `START` 守卫，否定消息会真实创建 `product-task:*`；
- 把 owned convergence 退回单次 `shield`，第二次取消会让调用方在 durable 终态前返回；
- 删除 `scheme="venv"`，真实 `CandidatePromoter.run()` 稳定得到
  `promotion-target-inspection-failed`。

第一次发布全量得到 `2388 passed, 5 skipped, 1 failed, 17 errors`。18 个红灯只有
一个根因：两个真实示例插件仍声明 `<0.7`。pip 以 `ResolutionImpossible` 拒绝四
Wheel 共存，L2 以 `candidate-traceh-dependency-incompatible` fail closed；没有吞错、
跳过或放宽验证器。修复后门禁如下：

最终提交的第一次远端 Ubuntu 3.12/3.13 CI 又暴露了两条 Windows 本地门禁无法
证明的平台夹具错误，而不是生产实现失败。Git Patch 夹具只用
`git update-index --chmod=+x` 改了 index；POSIX 上随后的 `git add -A` 会按真实文件
mode 把它还原，所以现在夹具同时给真实文件设置 executable bit。Benchmark 权限测试
则把包含 Agent writable Workspace 的 attempt 父目录路径误列为 evaluator secret；
POSIX `repr` 保留 `/` 因而失败，Windows `repr` 转义 `\` 才偶然假绿。当前测试只检查
真正冻结的 Review、Promotion、target 与 verifier 值，Workspace 仍在模型可见上下文
中，生产隔离合同没有放宽。Candidate L2 的失败只是递归全量碰到这两条错误后的连带
结果。修正后先运行两条精确反例，再由新的 GitHub Actions run 在 tag 前复验三个矩阵。

| 门禁 | 结果 |
|---|---:|
| 全仓 collect-only | 2411 |
| 最终完整 pytest | 2406 passed, 5 skipped |
| Product 主线 | 258 passed |
| Product Benchmark | 52 passed |
| Runtime/CLI cancellation 相邻组 | 79 passed |
| L4/Promotion/插件 CLI/版本相邻组 | 202 passed, 1 skipped |
| Plugin Creator 自身 | 10 passed |
| Python Quality 自身 | 17 passed |
| 四个真实 Wheel E2E | 18 passed |
| 从修复后 HEAD 运行的真实 L2 | passed |
| `compileall` / 修改范围 Ruff | passed |
| 文档链接、围栏、正式↔通俗映射 | passed |
| `git diff --check` | passed |

五个 skip 仍是 Windows 上四处目录 symlink 权限边界和一处路径不能包含 NUL，不是
本次变更引入的失败。

## 3. 真实模型与凭据边界

本补丁没有重复运行 18-attempt 真实模型质量网格。三项产品修复与插件元数据缺口
都由确定性宿主反例、真实 Git/venv/Wheel 和 durable facts 证明；重跑外部 Provider
既不能加强这些结论，也会引入网络噪声和费用。v0.7.0 的第七轮真实网格仍保留在
其验证记录中。

修复期间没有打印、复制、写入文档或提交任何 Key。一次本地 `traceh doctor` 诊断
曾调用正常 env loader，只输出配置字段与存在性，没有输出值、调用 Provider 或修改
`.env`；最终测试、干净构建和离线安装不依赖仓库 `.env`。

## 4. 干净构建与离线安装

发布资产只从包含本记录的最终发布提交，在仓库外的全新 clone 中构建：

- `traceharness_py-0.7.1-py3-none-any.whl`；
- `traceharness_py-0.7.1.tar.gz`；
- `traceharness-py-v0.7.1-source.zip`。

发布前要求 Wheel/sdist metadata、被导入的 `traceh.__version__` 和 CLI banner 均为
`0.7.1`；source ZIP 必须与该提交的 `git ls-files` 精确一致，并排除 `.env`、Git
元数据、缓存、bytecode、Session/Event 数据和未跟踪笔记。全新 venv 只从本地
wheelhouse 以 `--no-index` 安装核心及运行依赖，再运行核心导入、`traceh --help`、
无密钥 scripted `doctor`、`traceh eval --help` 和插件发现。资产 SHA-256 与精确条目数
记录在 GitHub Release 外部元数据中，避免让 source ZIP 内的本文件自引用其自身摘要。

## 5. 仍然明确排除

v0.7.1 不提供 OS sandbox、跨进程 Agent/Workspace 接管、冷恢复、stale claim
takeover、自动批准、真实远端 Promotion、通用 Workflow DSL、MCP、TUI、SQLite
EventStore、Memory 或 Provider retry/fallback。Product 配置仍是显式 opt-in；插件仍是
受信任的同进程代码，安装不等于启用。
