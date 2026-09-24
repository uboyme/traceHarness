# 078：真实仓库模型试跑与量化扩展

2026-09-20。承接[离线验收 077](077-real-repository-evaluation.md)和[执行计划](../plan/TRACEHARNESS_REAL_REPOSITORY_EVALUATION_PLAN.md)。

## 授权与冻结条件

用户明确指定百炼 deepseekv4.1flash，授权合理费用的真实调用；精确模型 ID 为 `deepseek-v4.1-flash`。
沿现有凭据装载器取连接，只冻结非秘密配置，不打印或保存凭据。未授权提交、推送或发布。

六组独立 paired Evaluation，每组三题之一、single/multi 各一次；第二次重复逆转题目与两臂顺序。
固定原有三个公开开发题，完整源树、原 Issue、隐藏测试、参考准入条件不变。不同重复通过单独目录区分，
不伪称远端模型随机种子可控。原始 Issue 自带修复建议也保留。不能声称模型未见过公开题。

共享整树额度 800,000 token；coder 760,000、100 steps、140 tools、1,260 秒，单 Turn 最长 900 秒；
coder 输出上限 16,384。最多两个 investigator，各 160,000 token、24 steps、32 tools、180 秒、输出 8,192；
保留主方 200,000 token。multi 是必经结构化分工且助手只读，未启用 patch_author，不代表所有协作策略。
较长助手授权不能装入 await_report 单次调用上限时，原工具提供可纠正拒绝并引导 dispatch_and_continue。

每组总期限 3,600 秒、关闭宽限 180 秒，模型 max_attempts=1。原 Provider HTTP 超时 120 秒，思考模式沿
百炼服务端默认（官方默认开启）；未固定远端模型修订与 reasoning_effort，作为复现边界报告。

[百炼官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)：北京忙时输入每百万 token 2 元，
输出（含思考）8 元。忽略优惠。12 × 800,000 × 8 / 1,000,000 = 76.8 元是配置额度的保守规划值，
不是账单保证：现有无 tokenizer 的预算预留会以余额封顶，不能保证请求真实输入和输出都装入余额；
超支检测发生在返回时。操作预算 100 元，每组后核查累计估算、Provider 失败、未知用量、收敛；
出现异常停止后续组，既有同组两臂仍由原 paired runner 管理。未知费用不得作为零成本。

第一笔调用前冻结 `.traceh/rr-eval/live-pilot-v1/` 下 material、sandbox.json、pair-01..06.json、
preregistration.json。原 Runner 分别再冻结源码、材料、实际 Provider 请求、事件与 worker 回执；
比较与证据复核复用原 owner，不新增评分器或账本。源码在运行期间保持不变。

## 执行记录

- 六份计划均通过原解析器、Runner 构造与材料复核；第一组 marshmallow single/multi 已启动。所有 12 个槽位在调用前登记，第一组尚未结束。
- 本轮是接线、预算与错误分类试跑，不将 12 次运行当 12 道独立题，不给收益显著性结论。
- 后续扩集与 Evaluator 校准在模型表现之外冻结选题、标签规则与分母，见计划；不依据结果挑题。

- 扩集元数据已固定为 dev 23 / test 300 的完整顺序队列，仓库不相交；目标开发 10 / 留出 30 尚未逐题准入。
  test Parquet SHA-256 为 `7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4`。
  直连下载超时，使用已有系统网络配置成功获取；这是材料获取问题，与百炼调用链分开记录。
- 校准规则和统计口径已写入计划 RR-6/RR-7，尚未产出软质量误判率或有界优化收益。


## 首组发现：测试替换前的误拒绝

v1 第一臂 marshmallow single 已结束，原报告为 failed/converged，28 次执行模型调用（另有 2 个 requester 请求快照），input 448,100、
output 16,618，精确 usage 合计 464,718，忙时估算 1.029144 元。候选只改 fields.py 与 test_fields.py；
忽略换行差异后的源码修复是 schema.opts → self.root.opts，另加三个 List/Tuple/format 回归测试。
宿主在 pytest 断言前因 test_fields.py 的原始哈希不同拒绝，故不能把该失败归因于模型未修好功能。
模型自己的 shell 测试与宿主固定测试是不同证据，原报告不改写。

根因归 Evaluation 材料 producer/oracle：同一个测试文件先被要求保持原字节，随后却必定被宿主冻结
版本覆盖；前置拒绝不能加强后续运行的测试权威性，却拒绝了合法补测试。原离线参考驱动只注入源码，
因此旧测试/验收未覆盖“正确源码＋新增回归测试”这一正常路径。

修正：oracle 对 `test_files` 中即将被完整覆盖的路径跳过原字节检查；其他 protected 支撑文件继续
要求原哈希。替换只发生在 Verifier 一次性沙箱副本，不回写候选，不采用候选自造断言作为通过依据。
这是被替换路径的一条通用规则，不写 marshmallow 题号或文件名特例。plan_version 升为 2，旧材料和
报告保留；尚未启动的 v1 后五组停止，修正后另起 v2，两个条件版本不拼成一个样本。

新增/调整测试覆盖：正确源码附加测试可通过；错误源码把测试换成恒真仍触发固定失败断言；未替换的
conftest 被改仍在导入前拒绝。26 项材料/仓库/架构检查通过，compileall 通过；反向撤回跳过规则时，
两个新用例因原误拒绝失败，日志保留 `.traceh/rr-eval/reverse-replaced-test-integrity.txt`。Ruff 通过。
新材料 `.traceh/rr-eval/pilot-v2-input/` 已通过原 parser；六项沙箱准入运行中。multi v1 仍执行中。


修正后的六项材料准入全部通过。另把 v1 single 的原 CAS Patch 按 SHA-256 校验后施加到相同初始树，
通过原 SandboxExecutionService 执行 v2 Verifier：77 passed / 0 failed / 0 errors / 0 missing，
沙箱收敛，候选 test_fields.py 前后哈希不变。证据位于 `.traceh/rr-eval/v1-single-counterfactual-v2/`。
这是同一候选的材料规则反事实诊断，证明原拒绝会误伤功能正确补丁；不是新增模型成功试次，不能改写 v1 成绩。


## v1 收敛与预算配置诊断

v1 pair-01 两臂已结束；single/multi Product 均 failed，全部 6 个账户关闭，6 个 Workspace 均非 live，
其中 2 个脏工作区 quarantine 留证。single 28、multi 43 次执行模型 attempt；Session 快照另各有两个
requester 请求。两臂共 75 份请求经原 Reader/Invariant/Reconstruction 复核，原库字节未变。

multi 的两个助手和主方各在一次返回结算时耗尽 token 余额：无 tokenizer 分支仅按余额截断预留，
没有先从余额扣除输入容量。对应三个响应的实际 usage 未进入可重读的 Attempt 终态，账本标 unknown，
因此原评估正确把 multi 完整 execution tokens 记为 unavailable，而不是零。此问题不同于 Provider 错误。

停止后五组 v1 和尚未调用的 v2。只读诊断从已冻结请求估计未知部分的保守规划容量：337,740 个
“canonical UTF-8 输入字节＋输出上限”，统一按输出价预算 2.701920 元；已知 usage 部分合计估算
2.650456 元。合计 5.352376 元是操作预算的保守规划值，不是假称完整实际账单或恢复了丢失 usage。
精确未知值仍保留，旧组不能计算完整每成功任务实际费用。

预算合同已有 `token_estimate` 配置与准入前 input/output shaping；此轮不增加平行计费路径或修改
生产 Budget/Runtime。v3 显式启用 cl100k_base、输入余量 100%，其余整树额度与模型/题面不变；
已知请求中实际输入 / 原 CanonicalTokenCounter 最大约 1.05227，仅作配置合理性诊断，不能保证未来。
余额不足 padded input 时由原 owner 在调用前拒绝；失败试次仍保留，不能为了 multi 成功无限加额度。
v3 是材料版本 2＋计数配置的新冻结条件，不将 v1 调试拼入其 12 次试跑。旧未知部分按上述保守值占用
本轮 100 元操作预算；若新条件再出现无法界定的用量/收敛异常，停止后续组，不继续改参数追分。

v2 原 Product 参考驱动的三份证据均完成重读（10 个请求快照），但核查报告后发现实际仅前两题成功，
astroid 在 workspace-git-failed 处停止，原报告 complete=false；不能把驱动进程退出与重读通过说成三题成功。
正在独立定位 Git 工作区失败。六项沙箱材料准入仍全部通过，但不替代 Product 主线验收。


v3 pair-01 single 已完成 Product/Workflow/Review/Promotion：135,953 input + 3,827 output = 139,780
精确 token，估算 0.302522 元。此时 multi 仍执行中。v1/v3 的 single 消费不同不能当作材料修正或输入
计数带来的成本收益：条件已变、远端模型无受控 seed，且只各跑一次。统计仍待同一 v3 条件完整重复。


Git 路径问题已在独立源码副本复现：受管 worktree 根路径长 165 字符时，Git worktree add 返回 128，
明确报 astroid 原有深层 testdata 目录 Filename too long；同树根路径 123 字符成功。未修改或删减源码，
未动全局 Git 设置，也未移动已有证据目录。原 v2 深目录离线失败保留，短根 `.traceh/r2` 重验进行中。

v3 已运行第一组保持原位置，后五组在首次执行前显式选择 `.traceh/p3/p02`～`p06` 输出根，
根路径缩短使现有完整树能被 Git 管理；布局映射冻结于 v3/output-layout.json。同题两臂使用同样目录层级，
模型、预算、材料、验证规则和评分无变化。目录位置是宿主环境准入条件，不把新尝试偷偷覆盖旧报告。


v3 首组 multi 已收敛但失败。此次不是返回后超支：主方侦察后分工，await_report 首次因 180s > 55s
被可纠正拒绝，随后异步派发在第二个助手的 retained_tokens 准入处失败，已启动的第一个助手被原
Supervisor 取消并收敛。取消发生在一次 HTTP 调用中，所以该响应精确 usage 不可用；3 个账户关闭，
3 个 Workspace 均无 live。原 usage=null 保留。不能把这次配置下的失败排除或换条件追成功。

已按组间停止规则停下并重读全部 16 个请求快照，确认该未知为有完整冻结请求的已收敛取消：
已知费用估算 0.594990 元，未知调用按 27,970 输入字节＋输出额度单位保守规划 0.223760 元。
说明此处“unknown 就永远结束实验”会截断正常的失败/取消样本；恢复同一条件后，批次控制保留原
usage unavailable，仅对已验证收敛且有明确输出上限的 cancelled Attempt 使用单独费用规划字段。
无法绑定请求、其他未知 Provider 结果、不可证明收敛仍停止。该组间对账细化在 pair-02 前记录；
没有改成功判据、模型/预算、候选规则或删除失败，完整费用仍不能冒充精确测量。v3 首组预算规划
1.121272 元，其中未知部分单列；batch driver 只调用原 CLI/inspect/load_run/recheck，不拥有另一套评分。


短根 `.traceh/r2` 的离线参考驱动报告 complete=true，三题 success 均 true。随后从原 product_review
packets/review_input 只读构造裁判输入（零模型调用）：marshmallow 923,168 bytes / 291,682 cl100k tokens，
pydicom 14,873,733 bytes / 6,586,957 tokens，astroid 2,180,132 bytes / 659,482 tokens。
原因是原包携带全部 original_files，二进制也转换为转义文本；这是现有 soft Evaluator 扩展到真实仓库的
输入规模边界，不能把它说成软质量已经评估。后续有界证据包应改在原 review owner，当前不动在跑源码。
测量是本地编码估算，不是远端窗口准入或收费记录，见 review-input-size-diagnostic.json。


修正离线 acceptance CLI 的退出约定：原先 Runner 返回便以 0 退出，导致 incomplete 报告也看起来成功。
现在同时要求原 report.complete 与期望 assessment（reference=passed、unfixed=failed），否则退出 1；
输出明确 measurement complete，不再把进程返回说成验收通过。四个公开 CLI 路径测试通过，撤回 complete
守卫时 incomplete 用例确定性失败；材料组现在共 17 项通过，修改范围 Ruff/compileall 通过。

校准计划补充分母：端到端硬门禁与实际送审裁判分开，必须加入硬测试通过但语义/质量不合格的样本。
当前生产只支持 status/reason，拟议 0–2 维度是人工诊断规则，未实现新评分协议。


v3 前两组已结束并重读：marshmallow single 成功 / multi 派发失败；pydicom single 成功 / multi
在后续 model admission 前耗尽 token 容量。pydicom single 为 334,166 input + 6,458 output = 340,624，
估算 0.719996 元；multi 为 630,303 + 25,959 = 656,262，估算 1.468278 元，完整 usage exact。
后者没有再出现返回后余额截断的未知；它是当前有界配置下的失败，不等于模型在无限预算下不能解题。
当前 4/12 已执行，第三组 astroid 开始。正式结果仍待同条件两轮完成。

astroid 第一轮 single 在 900,000ms coder turn 上限处取消，未进入固定 Review，不能报告为
“固定测试判错”。原报告 complete=true，success=false；22 次执行模型调用，313,062 input +
27,702 output = 340,764 exact token，全部账户关闭，脏候选工作区 quarantine 留证。原工具描述已经
明确 argv 不解释管道/重定向，但模型仍多次传入 `|`/重定向；另有一次容器中 Git 不存在的 start-failed。
这些属于实际工具使用/环境适配失败证据，不应凭直觉归因于补丁能力，当前不修改正在执行的条件。

第一轮三组均结束（6/12）：single 成功 2/3，multi 成功 0/3。astroid multi 的 22 次执行模型调用
全部处于 collaboration-plan，541,338 input + 53,705 output = 595,043 exact token；依次遭遇
assignment 字段无效、两助手额度超过余额、等待方式超出调用期限，调整为单助手异步后仍因
retained_tokens 准入失败而结束。没有助手真正进入模型执行，不能描述成“助手修复失败”。
第三组两臂共 48 份请求快照重读通过；两臂均未进入最终 Review。第二轮按冻结顺序先执行 multi。

新增派生诊断按原 request/view、model/attempt-end 和 execution session 身份统计阶段用量，
总 attempt/token 必须与原报告一致；无 view 的执行独立列出，requester 不混入付费执行。
marshmallow 第一轮 multi 分工阶段为 241,731 exact token，另有一个已取消助手调用 usage 未知；
pydicom 分工阶段 218,963，主方执行阶段 171,024，助手执行合计 266,275。阶段统计不会改写原评分。

第二轮 astroid multi 先执行，结构化分工通过，实际启动两个只读 investigator；这与第一轮未能派发
形成了同条件下的执行差异。三方后续均在下一次模型准入前因 max_tokens 容量不足结束，原
Product/Workflow failed，未进入最终 Review。37 次执行模型调用，599,711 input + 51,754 output =
651,465 exact token，全部 4 个账户关闭，4 个工作区无 live（1 个 quarantine）。原事件可分别绑定
两名助手及主方的 BudgetExhaustedError；不能将其说成助手已给出可验收修复。同题 single 正运行。

随后 astroid 第二轮 single 交回真实源码＋新增回归测试补丁，原 Product verification 在执行固定断言
前失败。`patch/review-recorded` 的 stderr SHA-256 精确对应
`protected test material changed: tests/unittest_inference.py\n`；冻结材料只替换运行用的
`tests/unittest_python3.py`，而把未选入的其余普通测试文件都列入 protected。原 Patch SHA-256
`2696cccbede368d2ffc46130f9206295258cc42d3f91544603eefc612e0c7c4f`，大小 2,406 bytes，
其中源码修复在 `astroid/nodes/node_classes.py`，另改上述测试文件。原报告 success=false、
review_passed=false，416,294 exact token；此处是材料层误拒绝，不能归因于固定断言未通过。

独立诊断 `.traceh/cf-a4/`：按哈希读取同一 CAS Patch，施加到相同 v3 初始树，只在一次性候选副本中
把**未参与固定测试**的 `tests/unittest_inference.py` 还原为原字节；源码修复和 v3 原 Verifier 完全不改。
原 SandboxExecutionService 执行得到 26 passed / 0 failed / 0 errors / 0 missing、exit 0。
这是反事实材料诊断，不能改写原 trial 或把 v3 的 12 次样本当无偏成功率。根因是材料将整棵 tests
目录一概列为 protected；前次 v2 只放行被替换的 test_files，仍漏掉未选入固定测试的正常候选回归文件。
后续需在材料 owner 明确固定测试和实际支撑文件的保护范围，保持测试规避反例有效；新规则须另起
版本并反向验证，不能在正在运行的 v3 上就地换评分。


## 两轮原报告和证据收口

六组 12 次全部执行，原 paired Runner 及逐臂 `load_run`、Product evidence、目标 Git ref、Session
invariants 与 request/snapshot reconstruction 重读通过：12 个 attempt，305 份请求快照；原 SQLite
摘要在重读前后未变。全部账户关闭、Workspace 无 live。请求快照含每臂两个脚本化 requester 请求；
执行模型调用数以原 execution 口径计，single 132、multi 149，共 281。

| 题目 | repetition 1：single / multi | repetition 2：single / multi | 关键解释 |
|---|---|---|---|
| marshmallow-1359 | 成功 / 分工失败 | 材料误拒绝 / 分工失败 | 第二次 single 改了未选中的 `tests/test_schema.py`，固定断言前被拒绝 |
| pydicom-1694 | 成功 / 预算耗尽 | 成功 / 分工失败 | single 两次完整推广；multi 的失败发生在授予或后续模型准入 |
| astroid-1196 | 900 秒 Turn 上限 / 分工失败 | 材料误拒绝 / 三方预算耗尽 | 第二次 single 改了未选中的 `tests/unittest_inference.py`，固定断言前被拒绝 |

按**原报告**统计 single 3/6、multi 0/6；这是版本 2 材料条件的原始执行结果，不是可比较的
模型成功率。两次材料误拒绝的原 CAS Patch 分别独立施加到相同初始树，先只在诊断副本还原那份
未选中测试，保持原 Verifier 不变，astroid 26/26、marshmallow 77/77 通过。后用 material
plan_version 3 对原 CAS Patch 原样重验，分别 26/26、77/77 通过。两条诊断都不产生原试次的
Product/Workflow/Review/Promotion 回执，因此不能改写 single 3/6，更不能事后填成 5/6。

single 6 次的执行用量完整 exact：1,693,393 token、按冻结单价估算 3.776528 元；含失败调用，
按原成功数计算的 1.258843 元/成功只是旧材料条件描述值，不应用于简历。multi 6 次已知 usage
部分估算 6.752994 元，另有 3 个已收敛取消的 HTTP Attempt 缺精确 usage，不填零；按冻结请求
字节和输出上限单列 0.708816 元保守**规划**值，multi 规划合计 7.461810 元，完整费用与
每成功任务费用均不可得。v3 两臂已知共 10.529522 元、规划共 11.238338 元；再计入 v1
停止前已知和未知规划 5.352376 元，本轮操作规划累计 16.590714 元，低于 100 元阈值。
这些均非供应商实际账单，也没有证明完整费用的数学上界。

原报告副本、逐臂证据重读、每组费用诊断、逐题阶段摘要与本地原 EventStore 哈希归档在
`docs/validation-data/real-repository-pilot-v1/v3/`；完整原 EventStore/CAS/Git 与失败候选仍在忽略的
执行目录。源码/提示、模型、材料及判分在 v3 六组期间未改；v3 的单多差异受已证实材料误拒绝
污染，且只有三道公开开发题，不能计算显著收益或替换简历的提升百分比。

## 材料 version 3 修复与独立准入

根因在 `tests/real_repository_evaluation/materials.py` 的材料生产：version 2 把整个 tests root 的
普通测试模块也列入 protected，只跳过选中并被替换的 test_files。当前每题在 selection 中显式
声明 `writable_test_patterns`，从 protected 哈希集合排除普通候选回归测试；候选自造测试仍不在
固定运行集，宿主选中测试完整替换；conftest、资源和配置继续受原哈希保护。材料 plan_version 升 3，
未修改原 Product/Evaluation 判分 owner，也未改 v3 执行目录或历史材料。

单测的正例为“正确源码＋改未选中测试”通过；反例为“错误源码＋候选测试”仍失败、“改 conftest”
在断言前拒绝；路径模式按路径段匹配，`tests/test_data/helper.py` 这种嵌套支撑文件不会被
`tests/test_*.py` 误放行。暂时撤掉新保护范围规则时，新正例如预期被原错误拒绝；恢复后材料组
18 项通过。反向日志 `.traceh/rr-eval/reverse-unselected-test-protection.txt`。新材料已重新核对完整三题源码树、
冻结身份与 format 3 Product 解析，六次原 Sandbox 材料准入全部通过（原版目标失败、参考通过）。
原 Product 主线另得到参考 3/3 完整推广、保留缺陷 3/3 拒绝；两组各 12 个请求快照独立重读，
所有账户/Workspace 收敛。两个原模型 Patch 不还原候选测试地经 material version 3 原沙箱验证
再次各自通过；原候选工作区保持原样，原试次仍为 failed。新材料与诊断摘要见同一验证目录。
收紧路径段匹配后重新生成材料，`dataset.json`、`benchmark.json` 和 provenance 与前一份 version 3
逐字节一致，故上述真实准入/对照仍绑定完全相同的冻结材料；当前生产者源码摘要与材料摘要均留证。

剩余边界：该固定 Python 测试集仍不等于完整上游测试或官方 SWE-bench harness；候选普通测试不
计分，明确支撑文件受保护，但不声称能抵抗任意恶意 Python 动态导入/篡改 pytest。扩大付费任务集
前仍需按 RR-6 完成逐题准入与协作预算可行性门禁，按 RR-7 建立人工软质量校准；当前没有多智能体
收益、有界进化提升或裁判误判率的可信数字。
