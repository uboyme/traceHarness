# 分层压缩 A/B：工具输出保存与真实读回验证

日期：2026-09-08。范围：ADR-0053 的 A 合同与 B 保存/目录/读回；不代表整个分层压缩计划完成。
未运行全量、L2–L4、Wheel、联网安装。未修改用户会话、配置或工作区；未提交/推送。

## 已验证的生产行为

原 ToolRuntime 将大正文/结构化数据完整保存到同一 Effect Outcome，同时提交预览与 digest 引用。
Session 携带有限预览，原结构化数据不再通过 Tool Result.data 重复进入 Session。
默认 PURE_READ 工具提供本会话输出目录和原文/data 分页；目录后续 offset 绑定 through_seq。
恢复、取消收敛和不变量检查复用原 owner 与同一来源校验，未新增数据库或后台存储生命周期。

工具调用的成功与进程退出码分开保留。保存失败不被伪装成工具执行失败；Outcome 已提交后恢复
同一个 Result，不重跑实际操作。原文读取页经过普通 Tool/Effect/Result 和请求快照，重放不重新读取。

## 最终定向门禁

| 门禁 | 结果 |
|---|---|
| Tool 保存/读取/恢复、Compaction、Surface、不变量、当前任务定位、Context、Plugin Runtime、History 与 reference 相邻回归 | 171 passed / 1 skipped，58.36 秒 |
| D0/SQLite/Product 架构、Context 请求协议拒绝、可选 TUI 入口 | 67 passed，3.78 秒 |
| compileall src tests、修改范围 Ruff、git diff --check | 通过 |
| pytest collect-only | 收集 3433 项，未全量执行 |

合计 238 passed / 1 skipped。唯一 skip 是 Windows 环境没有目录符号链接创建能力，来自
`tests/test_tools.py::test_workspace_symlink_component_is_rejected`，没有跳过新功能测试。

第一组具名文件：`test_retained_tool_output.py`、`test_tools.py`、`test_tool_runtime_failures.py`、
`test_tool_middleware.py`、`test_recovery.py`、`test_compaction.py`、`test_surface_and_invariants.py`、
`test_active_request.py`、`test_context_runtime.py`、`test_plugin_runtime.py`、`test_history_runtime.py`、
`test_reference_orchestration.py`、`test_reference_read_actions.py`、`test_reference_retention.py`。

第二组：`test_v07_d0_architecture.py`、`test_sqlite_event_store_architecture.py`、
`test_product_architecture.py`、`test_context_request_protocol.py`、`test_tui_optional.py`。

本地执行日志分别为 `.pytest-tmp-codex/retained-output-validation.txt` 和
`.pytest-tmp-codex/retained-output-architecture-final.txt`。collect-only 与检查日志位于同一临时目录。

新增测试包含：实际 Shell+SQLite 的成功/非零 exit、Unicode 与 JSON 转义页界、压缩和关闭重开后的
完整恢复、原始 data 读取、目录边界跨新输出不漂移、同工作区不同 Session 隔离、来源/digest 篡改拒绝、
长错误/工具自报 timeout、提交前失败、提交后错误、重复取消和幂等恢复。

## 反向验证与开发期失败

临时恢复“保存前截断正文”的故障，让真实 Runtime→Shell→SQLite 测试运行；新测试在
`assert expected in original['content']` 因缺少原文而失败。测试不是未启动子进程的空验证。
源文件在 finally 中逐字节恢复，正确实现随后通过最终回归。证据：
`.pytest-tmp-codex/retained-output-reverse.txt`。

开发中发现并修正的夹具/门禁问题：

- 恢复测试夹具缺少 Session 10/Context 9 必需的原始用户消息，尚未进入恢复逻辑就被拒绝；夹具补齐
  实际用户消息，未放宽生产来源校验。
- 首次真实 Shell 断言忽略 Windows stdout 文本换行转换；夹具改为显式写 UTF-8 原字节，检验逐字保留，
  未在生产代码中悄悄规范化输出。
- 默认工具列表增加两项后，Plugin 测试更新明确列表；Product 的 AgentRuntime SHA pin 经核查后更新，
  并记录仅装配同 SessionService 的两项只读工具的原因，保留其他三个 pin 与依赖检查。

## 真实模型旅程

入口：[真实验收脚本](../tests/live_tool_outputs/run.py)，用当前配置的真实 `qwen-plus`，
经原 Provider、Runtime、SQLite 和工具路径。密钥只通过已有加载器读取，未进入报告。
子进程运行时才生成随机标签/复核码，脚本文件本身没有答案；模型不知道期望的随机值。

每个场景有 260 条观测，原正文 22,939 字符 / 36,979 UTF-8 字节；验收显式使用 4096 字符显示阈值，
不是修改产品默认值。模型先运行一次诊断，回答预览之外的 R0164；宿主按原 M3 手动压缩，
关闭并重开 Runtime/SQLite 后，模型回答另一条 R0239；最后回答一个新算术问题。
问题未给工具名、引用 ID、分页偏移或期望答案。分别运行进程 exit 0 和 exit 7。

| 场景 | 新输出查证 | 压缩后关闭/重开再查证 | 新话题 | 实际诊断执行次数 |
|---|---|---|---|---|
| exit 0 | 正确读取随机标签/码，报告退出码 0 | 正确读取另一条随机记录 | 42 | 1 |
| exit 7 | 正确读取随机标签/码，报告退出码 7 | 正确读取另一条随机记录 | 42 | 1 |

最终 [第 5 次报告](validation-data/retained-tool-output/run-05.json)：6/6 用户回合通过，
21 次原文分页读取；每次查证都实际调用读取工具，恢复时实际查输出目录。请求重放和完整来源不变量均通过。
共 54 次正式 Provider Attempt，其中 37 次具有已知 usage，合计 361,862 tokens；其余 usage 不可用，
不能按零计费。测试显式使用原 retry policy 最多 6 次/360 秒、每次 Provider 超时 45 秒；用户配置未改。
五轮全部探索合计 144 Attempt，97 次已知 usage 共 594,898 tokens；47 次 usage 不可用，均保留且不按零处理。

不能据此声称任意模型都稳定。两个恢复回合都先失败读取过大的 History 页两次，之后才转向
输出目录；结果正确但有额外调用开销。当前未放宽 History 的整 Turn 页限制。

## 保留的真实失败与修订依据

[逐轮汇总](validation-data/retained-tool-output/runs-summary.json) 区分模型/工具行为与网络失败。

1. [第 1 次](validation-data/retained-tool-output/run-01.json)：20 次列目录被过严的夹具拒绝，
   夹杂 TLS EOF 后终止；没有实际运行诊断。最终日志汇总纠正了最初仅从末尾 TLS 错误判断的描述，不能算通过。
2. [第 2 次](validation-data/retained-tool-output/run-02.json)：夹具禁止了模型合理的列目录检查，
   模型反复调用被拒工具，最终网络失败；不能拿这次运行证明输出保存路径有效。
3. [第 3 次](validation-data/retained-tool-output/run-03.json)：修正夹具后实际执行并读取了三页，
   后续 TLS EOF，保留为未完成旅程。
4. [第 4 次](validation-data/retained-tool-output/run-04.json)：第一问正确；恢复时模型反复尝试过大
   History 页后宣称无法读取，没有使用已提供的输出目录。这是导航接线失败，不是网络问题。
5. 第 5 次：通用 reference guidance 明确区分 History 原话与保存的工具结果，并说明页正文直接位于
   Tool Result，而非下一 Step 的 Context receipt。沿用相同问题、页预算及随机原文断言完成两组旅程。
   没有加入场景名称、R0164/R0239 判断、答案或模型专用分支到生产代码。

## 文档与边界

正式版先更新，再同步通俗版，涉及 1、3、4、9/9.5、11、12、15、16、17 节；同步当前目录、
职责、流程图、配置、恢复、验证和限制。新增 ADR-0053 与分层计划，同步 README/CHANGELOG。
检查编号章节对应、相对链接、Markdown/Mermaid 代码块、秘密形态及反硬编码。

B 保留的是工具实际返回的文本/数据；不能补回底层工具自己截掉、未返回、或解码前丢失的字节。
磁盘占用、全 Effect 流读取、整批/整请求预算仍有限制。C 旧结果折叠、D 模型摘要、E 轮内控制和
TUI 新设置尚未实现。Product 角色能力不会自动扩权；未装配读取工具的宿主不宣称已有读取动作。
