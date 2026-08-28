# TraceHarness Py v0.7.0 验证记录

验证日期：2026-08-28

状态：发布候选打包门禁通过；tag、push 与 GitHub Release 尚未执行。

本记录描述 `v0.7.0` 的发布门禁。它补充而不改写
[`VALIDATION.md`](../VALIDATION.md) 和
[`validation-v0.6.0.md`](validation-v0.6.0.md)：旧文件仍是各自发布时点的历史快照。

## 1. 发布范围

v0.7 在 v0.6 已发布能力之上增加一条由既有 owner 组合而成的 ProductTask 主线：

- append-only 层级 Budget Ledger，以及在真实模型、Step、Tool、wall、process 与 child
  owned boundary 上的执行约束；
- commit-pinned managed Git Workspace、完整 candidate tree 的 immutable Patch Artifact，
  以及固定 Verification、不可改 Review、人工 Approval 和 bare Git ref CAS Promotion；
- Runtime 外的固定 Typed Workflow，不提供任意 DAG 或第二个调度器；
- `traceh chat --product-config` 的 Proposal、后续真人确认、single/multi/auto、durable
  进度、Approval 证据和按 ProductTask id 恢复的宿主控制面；
- 复用唯一 `traceh eval` 的 ProductTask Benchmark，以持久事实和宿主单调时钟度量
  resolved single/multi；auto 只报告路由并归入实际 arm。

本次没有给 `AgentLoop`、`AgentRuntime`、`ProcessAgentSupervisor` 或 `PluginManager`
增加产品分支，也没有引入第二 Product 状态、第二 Workflow、第二 Benchmark Runner、
旧 eval compatibility、自动批准或真实远端 Promotion。

## 2. 独立审查与自动化门禁

F4 两轮审查发现的 5 个 P1 和 2 个 P2 均有确定性公共路径反例，其中 6 项完成逐项
反向验证。F5 独立审查先发现 frozen command 与 Review 未绑定，复审再发现已有
Promotion 的 Product 恢复早退；两项都回到 Promotion owner 的共享 frozen-plan 规则修复，
并分别反向复现错误 ref 移动、错误 Benchmark 成功和错误 Product `completed`。

最终独立复审结果为 `P0=0 / P1=0 / P2=0`。其后只运行一次最终全量：

| 门禁 | 结果 |
|---|---:|
| 全仓 collect-only | 2407 |
| 唯一一次最终完整 pytest | 2402 passed, 5 skipped |
| Product（不重复 Benchmark） | 257 passed |
| Evaluation / F4 Benchmark | 52 passed |
| Budget / Workspace / Artifact / Promotion / Workflow | 397 passed, 3 skipped |
| CLI | 521 passed, 1 skipped |
| 0.7.0 版本合同与 Product/Promotion 架构 | 119 passed |
| `compileall` | passed |
| 修改范围 Ruff | passed |
| `git diff --check` | passed |
| 四个受保护核心文件 | zero diff |

五个 skip 是 Windows 上四处目录 symlink 权限边界和一处路径不能包含 NUL，不是本次
变更引入的失败。最终全量之后没有再重复运行全量；版本切换只运行版本合同及受影响门禁。

## 3. 真实模型 ProductTask 验收

所有历史网格均保留在仓库外，未补跑失败项、拼接报告或覆盖旧证据。当前 manifest 的
最终网格使用公开 `traceh eval`，运行 3 个不同任务 × `{single,multi,auto}` × 2 次；
provider、base URL、model 与 key 环境变量名均由显式输入决定，Key 只经现有 `.env`
loader 注入，未被打开、打印或写入报告。

WLAN DNS 修正后的第七轮结果：

| 口径 | 结果 |
|---|---:|
| attempts run / measured | 18 / 18 |
| unavailable metrics | 0 |
| strict full success | 16 / 18 |
| requested single | 5 / 6 |
| requested multi | 5 / 6 |
| requested auto | 6 / 6 |
| resolved single | 11 / 12 |
| resolved multi | 5 / 6 |
| auto strict parse | 6 / 6，全部 resolved single |
| DNS / TLS EOF failures | 0 / 0 |

剩余两次失败彼此独立：一次是远端在 HTTP response 前主动断开；另一次 multi coder
在 durable Session 已记录 126312 exact tokens 后，由 120000 的累计角色 Budget 在下一次
admission fail closed。没有为追逐 `18/18` 事后加 Budget、改题、重试或 fallback。

54/54 Budget account terminal；54 个 Workspace 为 52 released、2 quarantined、
`live=0`，全部收敛。报告 JSON/Markdown 的 18 条 attempt 与聚合逐项一致，且不含凭据
形态或当前机器路径。SHA-256：

- `report.json`：`a84a4e215d376673f3f50f775d2f47caf57cb04daf8691824a2809fa68cdd5d0`；
- `report.md`：`6470c3d80a902975319be12162c015731c9566fb7da243b87b187425cb287541`。

这些数字是固定条件下的小样本描述，不声称统计显著，也不把 auto 当成第三个质量 arm。

## 4. 安全与事实源检查

F5 安全扫描检查了 377 个 Git 跟踪或本轮预期新增的文本文件，没有真实 API Key、Token、
当前机器路径或 Benchmark/Provider 夹具渗入生产默认值。模型请求不含 approval/promotion
secret 由架构测试证明。`.env`、测试缓存、仓库外 Session/Event/Benchmark 证据、用户笔记
和审查草稿均不进入发布提交或资产。

按 no-example-hardcoding 纪律复核后，具体任务名和 provider/model 只存在于显式 Benchmark
manifest、确定性测试、历史验收说明或用户输入；生产实现没有把它们作为 fallback、默认
目标或歧义解析规则。

## 5. 候选提交、打包与离线安装

本节从包含本记录的候选提交的全新仓库外 clone 运行，结果为 **PASSED**。验证记录更新后
还要从最终候选提交再次构建，确保文档修订本身也进入 source ZIP；失败时不得 tag 或发布。

候选资产：

- `traceharness_py-0.7.0-py3-none-any.whl`；
- `traceharness_py-0.7.0.tar.gz`；
- `traceharness-py-v0.7.0-source.zip`。

完成的门禁：

- 从候选提交的干净 Git 输入构建 Wheel/sdist，源码 ZIP 只取 `git ls-files`；
- Wheel、sdist 与源码 ZIP 不得含 `.env`、Git 元数据、缓存、Session/Event、旧 build、
  `.pyc` 或未声明文件；
- metadata、`traceh.version.__version__` 与 `traceh --help` 的 CLI banner 均为 `0.7.0`；
- 使用本地 wheelhouse 与 `--no-index` 安装到全新、不含 `.env` 的虚拟环境；
- 在该环境运行 `traceh --help`、`traceh doctor`、`traceh eval --help` 和核心导入/版本
  验证；
- Wheel 为 190 entries，sdist 为 306 entries，source ZIP 与 `git ls-files` 精确一致，
  共 388 entries；必需的 ADR-0034、验证记录、Product inspection 模块和三个 Benchmark
  `.gitignore` 均存在；
- 离线 wheelhouse 只含核心 Wheel 和从本机已安装 `packaging 24.1` 的 Distribution 文件
  重组出的运行时依赖 Wheel；重组排除 `__pycache__`、`.pyc`、`INSTALLER`、`REQUESTED`、
  `direct_url.json` 和旧 `RECORD`，由 Wheel 工具重新生成 `RECORD`，全程不访问包索引；
- 全新 venv 使用 `--no-index` 安装 `traceharness-py==0.7.0`，确认 Distribution、模块版本和
  `packaging` 分别为 `0.7.0`、`0.7.0`、`24.1`；`traceh --help` banner、无密钥 scripted
  `traceh doctor`、`traceh eval --help` 与 `traceh plugins list` 全部通过；
- 最终候选提交与三项发行资产的 SHA-256 作为交付和 GitHub Release 的外部元数据记录。
  source ZIP 包含本验证记录，因此不能把自身最终摘要写回包内制造自引用。

## 6. 仍然明确排除

`v0.7.0` 不提供 OS sandbox、跨进程 Agent/Workspace 接管、冷恢复、stale claim takeover、
自动 retry、自动批准、真实远端 Promotion、任意 Workflow DSL、MCP、TUI 或模型流式输出。
Product 配置仍是显式 opt-in，不是默认 CLI 行为。发布候选门禁通过也不等于已经 tag、push
或发布；这三项必须另获授权。
