# 012：Sandbox S0–S4 执行记录

用户授权目标：顺序完成 S0、S1、S2、S3-A、S4；不跑全量或 L2，不提前做 S3-B/MCP/自由 Workflow。
当前已完成用户授权的 S0–S4 限定验收；未提交、推送、改版本、打标签或发布。完整证据见 [验收表](../validation-sandbox-s0-s4.md)。

## 当前完成范围

- S0：恢复已有 Docker Desktop 的临时 IPC 状态并使用已有镜像；13 项真实后端实验通过，见
  [验证记录](../validation-sandbox-s0.md)与 [ADR-0065](../adr/0065-host-owned-sandbox-execution.md)。
- S1/S2：不可变宿主执行单、原 CAS、原 stream 的 request/outcome/publication 已接通程序化 shell、
  完成验证和 Product 固定验证。文件写回先核对快照和权限，部分失败记录完成操作；不承诺目录级事务。
  原 Agent/Budget 准入及 Turn wall reservation 已用真实执行核对。Product START 的真实 Git/Docker 流程
  已到 awaiting_approval，未自动批准或推广用户仓库。
- 固定 Review 的原始输出不持久化；结构化结果绑定 execution/stream/receipt/policy，核对原执行 owner、
  命令、状态和字节摘要。Promotion 协议 2 拒绝旧 1，配置不自动迁移；Product 顶层协议仍为 1。
- CLI run/chat/resume/eval 接受显式 `--sandbox-config`，TUI 提供中文沙箱表单，复用唯一配置解析器。
  `/sandbox` 以原事件展示当前策略与历史实际后端/结束/收敛/回写回执，缺少 outcome 不冒充执行完成。
  真实 CLI 编辑、原 SQLite 证据及配置页保存/关闭/取消已通过。评估相邻 74 项中两处夹具在旧进程中失败，
  类型化 Review 结果与最后一组评估的显式 Sandbox 装配修正后，独立 2 项复跑通过；其中包含 11 个真实
  本地基准任务，使用 Scripted Provider，没有调用模型 API。未重跑基线模型题库或更改原 benchmark 成绩。
- S3-A：有界 stdio 接到原 executor/scope；原 Runtime prepare 只装配一个服务，Context 在 setup 期间
  才可按宿主精确插件版本授权启动进程。原 Activation 先登记 cleanup 再启动，原 Lease/Drain 负责寿命。
  首轮 46 项通过，加入观察与 Product 合同相邻后 140 项通过（86.454 秒），并非全量；包含真实旧租约
  继续收发、setup 回滚/取消、重复 shutdown、Agent Tool/Effect 与重放、清理失败持久化。
  输入关闭后程序立即退出的控制确认竞态已经修正：核对原终态，不重发 EOF。版本授权已反向证明：
  临时移除匹配保护，错误版本实际启动服务器，公开拒绝断言失败；测试先回滚真实资源，随后恢复保护。
  文件/TUI 插件授权与剩余 S3-A/S4 生产验收已完成；最终 463 项、观察补查 4 项和部分写入取消 1 项通过。

没有迁移或删除业务数据，没有新增兼容执行路径。shell/CommandVerifier/默认固定验证已经移除宿主
进程回退；程序化调用须显式配置本地镜像、Docker context、文件范围及资源额度。

## 验证纪律

真实实验只用临时目录、显式镜像、假环境变量和固定测试程序。协议测试覆盖路径拒绝、预算、
重复执行身份、请求与结果身份绑定；真实执行器检查覆盖成功、内部期限、缺失后端和启动后的重复取消。
硬链接拒绝已做按预期失败的反向验证。S2 Review owner 和策略一致性两条保护也完成反向验证：
真实执行先成功，移除对应保护后公开调用的拒绝断言出现 DID NOT RAISE，恢复后通过。
当前相邻 109 项（Promotion ledger/Budget/Runtime factory/middleware/Plugin runtime）及 74 项
Product 配置/装配/registry/读取检查通过；不把交叠集合或历史结果累加成一次全量。
最新沙箱相关 9 文件 59 passed（94.232 秒，真实 Docker），配置表单/Line 相邻 51 passed。
随后配置/Line/Product 合同与架构合并 8 文件 176 passed（56.179 秒）。两份 Runtime 源码指纹保护
在核对仅为原 verifier 接缝与装配注入后同步；没有解除保护。UI 点击助手改为等待真实动画完成再定位，
避免在折叠布局尚未稳定时错误点击，仍要求实际 click 命中并核对保存结果。
其中首次 CLI 用例误把 main 的 SystemExit(0) 当返回值而失败，修正测试调用合同后真实主线重跑通过；
这是测试夹具修正，不作为生产缺陷或安全反例。取消清理原因丢失则有真正反向证明：移除 cause 传播后，
真实命令已经执行，公开取消调用丢失清理异常，新增断言失败；恢复保护后通过。
旧 native 私有管道/进程锁测试已改为公共原 owner 的容器测试，覆盖中文输出、期限输出、实际子进程
启动后的重复取消、输出洪水、清理失败与独立解释器退出。原 Tool 大输出保留/折叠相邻测试也已通过。
生产验收还修复父命令退出后被后台管道拖到超时、观察器遗漏 Session 验证回执；均先真实失败复现。宿主硬退出后内部期限收敛、账本仍保持未知，也有实际进程证据。

当前可复核汇总见 [S2 定向记录](../eval/sandbox-s0/s2-checks.json) 和
[文档检查](../eval/sandbox-s0/doc-qa.json)。中途记录保留原阶段状态；最终结果以 [验收摘要](../eval/sandbox-s0/final-checks.json) 为准，不把历史交叠集合相加。
