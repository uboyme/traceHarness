# v0.11.0 限定发行验证

用户授权 AO-3 提交并发布后继续 DA-1～DA-5。本版收口尚未提交的 UE-0～UE-4、AO-0～AO-3；DA 与 MCP 不在本次发布范围。继续使用 Educational alpha 与 GitHub Release 渠道，不上传 PyPI。用户禁止全量 pytest 和 L2–L4，因此不声称通过完整发布门禁。

## AO-3 真实小样

- qwen-plus 直连，一轮隔离 Skill Chat、一次受限提案、两道题各跑两臂、四次原语义裁判。
- 共 10 个 Session、21 次模型请求、126,191 exact tokens，失败请求 0；原请求与不变量独立重放通过，757 个原文件未改变。
- 模型评分 1/2→2/2；任务 Token 27,820→34,498，工具调用 3→4。候选超过冻结的 1.15 倍 Token 门槛，因此 not-qualified，保留当前策略，不自动采用。
- 前台 26,062 tokens、分析 5,885、任务评估 62,318、裁判 31,926。小样不是新 72 题 benchmark，也不能证明泛化提升；裁判仍存在 AO-2+ 记录的偏差。
- 原文件与派生摘要见 [AO-3 数据](validation-data/unified-evaluation/ao3/README.md)。真实运行后另修复立即退出的准入收尾竞态，并增加原路径定向/反向验证；没有为 UI 与生命周期调整重跑整套 benchmark。

## 当前检查状态

发行定向首批 318 passed / 12 skipped，在一个过期的 0.10 版本重复断言处停止；删除两处重复版本断言、由原 test_version_contract 唯一检查 0.11 版本后，架构/版本/TUI 及相邻合同批次为 110 passed / 18 skipped。未修改已经通过的执行源码，未把两批相加；跳过项是未显式配置的 Docker 测试。另显式使用现有测试镜像，Product 两臂经过真实 Git、Docker 验证与原推广链，1 passed。编译、修改范围 Ruff、collect-only 3,978 条、diff-check 和文档检查通过；五项关键保护的反向检查均因预期根因失败。新 wheel 离线安装后 316 个包文件与源码逐字节一致，CLI/doctor 正常。没有运行全量 pytest 或 L2–L4，也没有把模型语义判断当成人工评分。

用户已授权提交并发行；提交使用 `[skip ci]` 防止远程 CI 自动运行全量。发行页为 [v0.11.0](https://github.com/uboyme/traceHarness/releases/tag/v0.11.0)，上传以该页实际资产为准。源码包包括测试与 benchmark，排除秘密、运行缓存和用户无关未跟踪笔记。

发行核查补充：独立 worker 回执现为 format 2，记录实际 pid 与 parent_pid；宿主进程记录 owner_pid 与启动 pid。原比较器验证“宿主直接启动 worker”或“宿主启动器→worker”的同一进程所有权链，支持 Windows venv 启动器而不忽略身份。请求摘要、冻结源码、环境、报告及回执摘要仍全部核对。旧 format 1 实验需用其归档冻结源码检查，当前比较器明确拒绝，不改写旧记录。证据不可比较时后台保留未知成本并停止，TUI 显示证据不完整，不把缺失统计当作零。

最终 0.11.0 wheel 在独立 Windows venv 又运行同一规模的小样：10 个 Session、21 次请求、125,128 exact tokens、0 失败，759 个原文件不变且请求重放通过。任务模型评分仍为 1/2→2/2，任务 Token 34,376→28,090、工具 4→3，本轮达到原冻结门槛并进入 review_candidate，未自动采用。前台 24,729、分析 5,942、任务 62,466、裁判 31,991 tokens。这是另一轮独立小样；前一轮成本超标记录仍保留，不拼分、不声称普遍改善。
