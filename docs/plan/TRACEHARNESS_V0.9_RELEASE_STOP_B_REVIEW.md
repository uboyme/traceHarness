# TraceHarness v0.9 Release Stop B 独立审查

日期：2026-09-07。对象：v0.9 F3 项目归属与 append-only Memory authority。

## 1. 结论

**修复后独立复审 P0=0、P1=0、P2=0，B-P1-01 已关闭，Release Stop B 通过；F4 未开始。**

第 2–6 节保留首审的冻结源码、原始反例与验证口径；首审只登记文档，没有修改生产代码或测试。
用户随后授权修复，实际变更、反向证据和修复阶段限定验证见第 7 节；另行授权的修复后独立复审见第 8 节。首审 P0=0/P1=1/P2=0 未通过的历史结果保留，不回写为绿色。
P2=0 表示没有另外登记的 P2；同一根因造成的合法 checkout 误拒列在 B-P1-01 下，不重复计数。

## 2. 冻结范围与边界

- 基础 HEAD：`22799a3de15890fe92f8a478edea366543428e6e`。工作树还包含 F1/F2 和 Stop A 修复，
  因此不把全部 `git diff HEAD` 当成本轮 F3。
- 按 F3 实现前后 SHA-256 清单锁定 36 文件：19 个生产 Python、10 个测试/夹具 Python、7 份文档。
  登记文档前 36 文件均与 F3 完成快照一致；登记后 29 个 Python 文件仍逐字节一致。
- 生产范围：`api/memory.py` 与 API 导出；`projects/`、`memory/`；`tools/memory.py`；
  `runtime/memory_control.py` 与 Runtime 装配；`product/project_scope.py` 与 host 接线；
  `workspaces/local_git.py`、`workspaces/supervision.py`；History 的闭合 Turn 共享判定。
- 公开入口：ProjectScopeService 的 create/bind_source/bind_session/resolve；MemoryService 与
  runtime.memory 的 read/propose/declare/approve/supersede/revoke；真实 Runtime 中的
  propose_workspace_memory Tool；Product host 经 Workspace Supervisor 的 create/resume/send。
- 检查现有的身份、Store/Session/resolver owner、exact proposal 与 predecessor、CAS、幂等、
  来源边界、owned append、重复取消、Drain 和创建失败清理。进程内 Plugin 仍为 trusted；不把任意
  敌意 Python 对象或 OS sandbox 当作新增威胁模型。
- 不审未来 F4 检索/Context 注入、F5 UI/质量评测，不提前实现这些能力。common-dir 路径摘要不是
  永久仓库 UUID；本反例不涉及同路径重建仓库、跨机器迁移或未承诺的永久 identity。
- 未读取真实 `.env`、Key 或其他秘密，未读取/修改用户的其他未跟踪笔记与目录。

## 3. B-P1-01：configured source 路径豁免了 linked worktree 的双向身份校验

位置：[LocalGitWorkspaceProvider.project_fingerprint](../../src/traceh/workspaces/local_git.py)，
审查时 561–567 行。文件 SHA-256：
`56359fd97fb3596dc9a422c68dd472abf4c9bd0d59d4a24500115c1f3a07809f`。

### 3.1 公开路径与确定性复现

所有名称均是显式测试夹具，不是生产默认。使用临时目录和真实 Git，无网络或真实 Provider。

1. 用现有 `test_local_git_workspaces._repository/_git` 创建一个普通仓库及两个合法 detached linked
   worktree，记为 A、B。宿主显式配置 `sources={"source-orion": A}`，不是把主 checkout 配为 source。
2. 用真实 SessionService 在 A 创建 requester；ProjectScopeService 创建项目、绑定 source/Session。
   这一配置实际被生产入口接受。MemoryService 提出并批准一条事实，再提出第二条待批准事实；
   此时 Memory head=3、active 数=1。
3. 保存 A 的 `.git` 原始字节，用 B 的 `.git` 内容覆写 A 的 marker，保持原 Git registry 不变。
   两者 common-dir 相同，A 仍出现在 `git worktree list` 中；但 Git 实际解析 A 得到的是 B 的 admin
   directory，registry 的 A entry 仍反向指向 A。通过真实 `git rev-parse --absolute-git-dir` 验证
   操作已经发生，不以夹具错误或未执行的 mutation 充当反例。Windows 隐藏 marker 使用 `r+b` 写入。
4. 再走 `MemoryService.read("requester")` 以及对第二条 proposal 的 exact 宿主 approve。
   **两者均成功，追加 `memory/approved`，head=4、active 数=2。** 当前损坏的 checkout 身份没有阻止
   读取和人工权限操作。最后在 `finally` 中逐字节恢复临时 A 的 marker。

实际输出（省略随机临时目录）：

```json
{"probe":"configured-linked-source-marker-swap","before_active":1,"after_active":1,"memory_head_after_approval":4,"active_after_approval":2,"actual_admin_changed_to_sibling":true}
```

补充公开反例：恢复 marker 后，为同仓库主 checkout 创建另一 Session，再调用同一 scope 的
bind_session，得到 `WorkspaceGitError`。合法主 checkout 被误当成必须具有 `worktrees/*/gitdir`
entry 的 linked worktree。这是同一分类错误的另一结果，不另记 P2。

### 3.2 根因、合同与影响

代码使用 `workspace != source` 决定是否核对 registered admin/backpointer；**宿主配置的 source
路径并不证明它就是主 checkout**。配置为 linked worktree 时，source 自身跳过双向核对；反过来，
合法主 checkout 因路径不同又被送入 linked worktree 检查。

[ADR-0044](../adr/0044-host-owned-project-scope-and-memory-authority.md) 与
[设计合同 §6.1](TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md#61-projectscatalog) 要求当前 Session
每次使用真实 checkout 证明归属，显式配置或相同路径不能代替证明；
[正式上下文 §7.6](../note/project-context.md#76-f3项目归属与-append-only-memory-authority)
还明确承诺拒绝互换 `.git` 指针。反例发生在当前允许的真实 Git 输入上，绕过当前访问前置条件，并
实际写入新的 active Memory，属于身份/归属与人工决策边界的 P1，阻断 Stop B。

没有证据表明模型直接获得审批能力、发生跨项目读取或 Git ref 推广；不扩大本 Finding 的影响。

### 3.3 现有测试缺口与修复 owner

`test_real_git_binding_is_read_only_accepts_dirty_source_and_rejects_copies` 始终将主 checkout 配为
source，交换的是两个非 source 的子 worktree，所以只进入正确执行双向检查的分支。真实 Product
用例同样从主 checkout 配置 source。六个 F3 文件全部通过并不能覆盖这个配置组合。

修复应由既有 LocalGitWorkspaceProvider 的 source/checkout 身份读取负责：根据实际 Git admin 与
common-dir 的关系区分主 checkout 和 linked worktree；对 linked source 和普通 linked consumer
采用同一双向规则，不按 configured source 路径豁免。还应覆盖以 linked source 访问合法主 checkout、
source mapping-only 读取、损坏 marker 拒绝及原取消收敛；不增加第二项目事实源或 Workspace 生命周期。

后续修复必须加公开 read/approve 零写入反例、合法源/consumer 组合回归，并临时恢复故障分支，确认
测试因真实身份豁免失败后再恢复修复。本轮未修改源码，因此没有运行修复反向验证，也不宣称已关闭。

## 4. 已验证事实

| 分区 | 本轮核查与定向验证 |
|---|---|
| Memory authority | 模型只能 proposed；宿主绑定 exact proposal/head/slot；supersede/revoke 核对前任；同槽唯一 active、已消费身份不复活；fresh detached replay |
| 并发与取消 | 相同操作并发返回原 receipt；竞争审批只有一个 active；append 前/后失败与 unknown 精确对账；重复取消等待 owned worker；Runtime dispose 等待 Memory Lease |
| 来源与权限 | 当前 Session 闭合叶、同项目 Product 原流终态、host declaration 分流；跨项目/Session、未知协议、篡改 digest、已识别秘密与私有输入拒绝 |
| Runtime/Product | 模型参数不能传项目/槽/审批；禁用默认工具不增加 Tool；Store/Session/resolver owner 错配零副作用拒绝；真实 Product 首次 Provider 派发前已有绑定；绑定失败/取消复用 dispose |
| 历史与存储 | released checkout 保留历史证据但失去当前访问；SQLite 重开重建相同 Memory；不新增 canonical table；历史请求不被后续批准改写 |

以上是已覆盖路径的事实，不抵消 B-P1-01，也不证明所有 Git 配置正确。

## 5. 本轮验证、未运行门禁与基线

Windows / Python 3.13。只运行以下 11 个具名文件；测试使用仓库外独立 basetemp，不使用 xdist。

| 批次 | 文件（均在 tests/） | 实际结果 |
|---|---|---|
| F3 核心 | test_memory_authority.py、test_memory_convergence.py、test_memory_runtime.py、test_memory_product.py、test_memory_sources.py、test_project_scope.py | 100 项全部通过，退出码 0，无 skip；真实 Product 用例 25.60s |
| 相邻 owner | test_product_architecture.py、test_product_contract.py、test_workspace_architecture.py、test_sqlite_event_store_architecture.py、test_history_reader.py | 134 passed in 2.91s，无 skip |
| 同一具名集合 collect-only | 上述 11 文件 | 234 collected in 0.71s；两批不重叠，共 234 通过，并非一次全量运行 |
| 静态检查 | 29 个 F3 Python 文件 Ruff、19 个生产文件反示例硬编码扫描 | 通过，无示例身份进入生产实现 |
| 新增公开反例 | 临时真实 Git → ProjectScopeService → MemoryService | 两次独立临时仓库运行均出现损坏身份仍可读取/写入；后一次进一步证明 approve 新增 active。伴随合法主 checkout 误拒 |

核心命令使用 `python -X utf8 -m pytest -q`；项目 addopts 已含 `-q`，故输出为逐项进度和 durations，
不虚构不存在的总耗时。相邻与 collection 显式用 `-o addopts=`。逐项集合收集确认核心 100 项。

文档 QA 通过：10 份文档、514 个有效相对链接、32 个闭合 Mermaid 块，两份上下文 0–20 章节对应；
Memory 配置字段和阶段测试清单一致，无新增秘密形态或编码损坏。`git diff --check` 通过。

反例脚本首稿曾因 Windows 隐藏 `.git` 的 `wb` 打开方式报 PermissionError，没有完成目标操作，
该次运行不算 Finding 证据；改为 `r+b` 后才得到第 3 节的实际结果。这是探针问题，不是源码或基线失败。
本轮没有新的既有测试失败、网络故障或单独的 HEAD 基线问题；F3 实现阶段的历史失败/修订记录仍保留
在阶段计划 §10.5，不计作本轮执行。

**未运行：**全量、L2–L4、Wheel/安装、联网包索引、真实 Provider/API、其他 OS、F4 检索质量门禁。
本轮源码与仓库测试未改，未重跑实现阶段 compileall 或修复反向保护。没有 commit/push/tag/release。

## 6. 文档同步与下一步

先同步正式上下文，再以其为依据同步通俗版第 1、7、15、16、20 节：当前 Stop B 状态、linked source
的真实限制、本轮测试口径、未关闭 P1 和禁止进入 F4。更新阶段计划、设计合同当前实现说明、总计划、
ROADMAP 与 CHANGELOG。ADR 的历史设计决定不改写；现有 Mermaid 的 owner/事实源结构没有变化。

首审要求修复 B-P1-01 并完成对应定向回归和复审；后续授权修复已完成，见第 7 节。
修复阶段当时仍待复审，后续结论见第 8 节；用户禁止的全量和 L2 不因“停止点”自动获得授权。

## 7. B-P1-01 修复与定向确认

### 7.1 当前变更与所属边界

只修改 [LocalGitWorkspaceProvider](../../src/traceh/workspaces/local_git.py) 与
[项目 scope 测试](../../tests/test_project_scope.py)。provider 在读到当前 source/common-dir 后读取
Git worktree registry，source 自身先经过 `_require_project_registration()`；即使 workspace=null
只查 mapping，也必须证明 source。不同消费方再验证路径、top-level、common-dir 和同一注册规则。
相同 source/consumer 只复用本次已经完成的证明，不按路径相等豁免。相对输入保持明确拒绝。

Git worktree list 的规定顺序是主 worktree 在首项，原 `_worktree_entries()` 保留该顺序。
主项实际 admin 必须等于 common-dir；linked 项必须等于既有 `_registered_admin_directory()`
按唯一 worktrees/*/gitdir 反向指针找到的 admin。这样 source 本身是 linked、消费方是主 checkout
或 linked 都有真实证明；linked marker 指向 common 自身也不能假充主 checkout。缺失注册或不符
明确拒绝，不增加路径 fallback、Store 事实、事件字段、缓存、协议版本或 Workspace 生命周期。
所有 Git I/O 继续由原 runner/process convergence 负责。

provider 修复后 SHA-256：`0a02fe34a6b451daa164f69fed1c59ad89ea51ab1d08a518ac5c2e60461af3cd`。

### 7.2 新增 13 项公开验证与反向证据

- 4 项注册组合：主／linked source 服务同仓库主与两个 linked Session，允许 dirty、只读且不创建
  managed root；无法由 Git registry 证明的 separate-git-dir 主目录保持绑定拒绝。相对输入仍拒绝。
- 4 项 marker 反例：替换为兄弟 admin 或 common admin 后分别直接调用 Memory read 与 approve，
  都拒绝，Memory 流逐事件不变；恢复 marker 后仍只有原 active。approve 用例直接调用生产方法，
  没有让夹具先 read 失败来冒充审批拒绝。每次先用真实 Git 证明 marker 已改变 admin。
- 3 项 mapping-only 边界：坏 source 阻止 project_fingerprint(None)、resolve_evidence 与首次
  bind_source；没有新绑定写入，恢复后 mapping 仍正确。
- 2 项取消：在实际 admin 检查的子进程 spawn/wait 边界放入真实就绪的管道阻塞子进程，用 Event
  定位时序，重复取消。调用返回前必须已回收子进程，Memory head 保持 1、项目 head 保持 3。
  这是 Git I/O 边界的实际进程替身，不把它称为真实 Git 命令挂起；没有用 sleep 猜时序。

临时将 provider 按原字节恢复为第 3 节的故障版本，运行新增注册正常组合与 marker/mapping 9 项：
**8 failed / 1 passed / 16 deselected in 28.17s**。其中 1 项是 linked source 下主 checkout 误拒，
7 项是实际错误身份路径未拒绝（DID NOT RAISE）；正常主 source 组合仍通过。没有导入错误或未发生
的操作充当反向证据。随后逐字节恢复修复 provider，并运行第 7.3 节的最终限定集合。

### 7.3 最终门禁与明确清单

同一具名 11 文件：

- test_project_scope.py、test_memory_authority.py、test_memory_convergence.py；
- test_memory_runtime.py、test_memory_product.py、test_memory_sources.py；
- test_local_git_workspaces.py、test_workspace_supervision.py、test_workspace_catalog.py；
- test_workspace_architecture.py、test_product_architecture.py。

**190 collected in 0.71s；189 passed / 1 skipped in 135.26s，退出码 0。** 新增 13 项全部包含
其中，不重复相加。唯一 skip 是 test_dangling_symlink_target_is_quarantined_not_replaced：
Windows 环境无法创建目录符号链接。实际 Product 首次派发、历史已释放证据、binding 失败取消、
Memory CAS/权限/来源/Runtime Drain 及相邻 Workspace owner 回归均在此集合中。

执行从仓库外 cwd 传绝对测试路径、独立 basetemp、`-o addopts=`，不加载真实 .env。
compileall src/tests、两修改文件 Ruff、生产文件防示例硬编码扫描通过，无示例词进入通用实现。

初次新增测试执行为 21 passed / 4 failed（73.93s）：两个 separate-git-dir 正向假设与两个
取消夹具的就绪信号失败。实际 Git list 不包含该主目录，当前合同不支持靠路径猜测，因此将此
布局验证为明确拒绝，不放宽生产检查；子进程 print 在 Windows 输出 CRLF，夹具按行尾读取修正。
随后对应 6 项通过（21.18s），并包含在上述最终完整限定集合中。这些是开发中夹具问题，不是
源码新 Finding、已有 HEAD 基线故障，也没有用放宽协议、skip 或缩小最终集合掩盖失败。

### 7.4 当前状态与边界

此阶段完成 B-P1-01 实现修复及定向确认，当时不声明停止点通过；后续独立复审已完成，见第 8 节。F4 未开始。
两份上下文第 1、7、15、16、20 节与阶段计划、设计合同当前实现说明、总计划、ROADMAP、CHANGELOG
同步。ADR 历史决定和现有 Mermaid owner/事实源结构不改变。

common-dir 路径摘要仍不是永久 UUID；Git 注册不能证明的布局拒绝；读到的关联不构成跨流原子
快照或跨进程 Workspace lease。未跑全量、L2–L4、Wheel/安装、联网或真实 Provider/API；未提交、
推送、打 tag 或发布。


## 8. 修复后独立复审

### 8.1 结论与冻结范围

**P0=0、P1=0、P2=0；B-P1-01 关闭，Release Stop B 通过。** 本次为单独的复审轮次：
从冻结的修复源码、当前公开合同、生产调用与新运行的反例出发，不把上一轮完成报告直接当证据，
也不宣称由另一名审查者或子 Agent 执行。没有新增 Finding，不扩大为 F4/F5 或任意敌意 Python 对象审查。

HEAD 仍为 `22799a3de15890fe92f8a478edea366543428e6e`。以 F3 36 文件清单和修复前备份区分既有
F1/F2 工作树与此次修复，实际生产修复只有 local_git.py，仓库测试修复只有 test_project_scope.py。
修复 provider SHA-256 仍为 `0a02fe34a6b451daa164f69fed1c59ad89ea51ab1d08a518ac5c2e60461af3cd`。
复审开始与最终验证后，29 个 F3 Python 文件逐字节相同；本轮未改源码或仓库测试，只登记文档。

### 8.2 语义核查与独立公开探针

| 核查点 | 证据与结论 |
|---|---|
| source 自身不能豁免 | project_fingerprint 先证明当前 source 注册与 admin，再处理 consumer；None 和相同路径都不能跳过 |
| 主／linked 分类 | 本机安装的 Git 官方 git-worktree 文档规定主项在首位；现有 NUL parser 保留顺序并拒绝重复路径。主项与 common 核对，其余走既有唯一反向指针规则 |
| 现有生产主线 | bind_source、bind_session、resolve、resolve_evidence 均调用同一 resolver；MemoryService 的 read 与 _decide 前置读取没有替代身份路径 |
| 正常与反例 | 主／linked source 的合法组合、dirty checkout、缺注册、复制／互换 marker、mapping-only、released 历史取证与当前访问隔离均在限定测试中 |
| 失败、取消与 owner | 新 Git I/O 仍由原 _GitRunner/converge_process 管理；spawn/wait 实际子进程重复取消、Memory append 对账、Runtime Drain、Product bind 失败收尾均通过 |

另在仓库外建立 test_review_source_identity.py，重新经真实 Git、SessionService、ProjectScopeService
和 MemoryService 构造 4 项参数组合。消费方分别为主 checkout／兄弟 linked checkout，坏 source
marker 分别指向主 admin／兄弟 admin。先验证正常绑定、已有 active 和待审批 proposal，再用真实
rev-parse 确认 source admin 已改变、consumer admin 未改变。随后分别直接调用 read、approve 和
resolve_evidence，均拒绝，projects:catalog 与 Memory 流逐事件不变。finally 恢复原 marker；
重试原来被拒的同一 exact approval 后成功，Memory head 3→4、active 1→2，没有预先消费操作 id。
只调用公开入口和检查持久／用户可见结果；未用先 read 的审批夹具替代直接 approve。

修复阶段反向验证的 8 failed / 1 passed 日志已逐项核查：7 项为错误身份未拒绝，1 项为合法主消费方
误拒；修复 provider 与恢复备份逐字节相同。这是第 7 节已有证据，本轮未临时改回旧源码或重跑该反向集合。

### 8.3 本轮执行与未运行门禁

限定第 7.3 节同一 11 个仓库文件，再加上述 1 个独立探针文件（4 项），共 **194 collected in 6.05s**。
最终 **193 passed / 1 skipped in 170.52s，退出码 0**；新增修复 13 项和独立探针 4 项均已包含，
不重复相加。唯一 skip 为 test_dangling_symlink_target_is_quarantined_not_replaced，原因仍是
Windows 无法创建目录符号链接。运行环境为 Windows / Python 3.13，仓库外 cwd、绝对文件清单、
独立 basetemp、显式清空 addopts，不使用 xdist、不读取真实 .env。

29 个 F3 Python 文件 Ruff 通过。源码和仓库测试未修改，因此没有重跑修复阶段 compileall 或反向
mutation；原修复结果与本轮实际运行分开记录。没有新测试失败、网络故障或需要归因的 HEAD 基线失败。
文档同步先正式版、后通俗版，覆盖第 1、7、15、16、20 节以及阶段计划、设计合同当前状态、总计划、
ROADMAP 与 CHANGELOG；ADR 历史决定和现有 Mermaid owner/事实源结构未改变。
文档 QA 通过：10 份文档、522 个有效相对链接、32 个闭合 Mermaid 块，两份上下文 0–20 章节对应；
Memory 配置和阶段测试清单一致，无新增秘密形态或编码损坏，git diff --check 通过。

**未运行：**全量、L2–L4、Wheel/安装、联网包索引、真实 Provider/API、其他 OS 和 F4 检索质量门禁。
common-dir 摘要仍非永久 UUID，Git registry 无法证明的布局明确拒绝；不新增跨流原子快照或跨进程
Workspace lease。停止点通过仅表示当前冻结范围审查清零，不代表 v0.9 发布门禁完成。本轮未进入 F4，
没有 commit/push/tag/release。
