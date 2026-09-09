# 分层压缩 B+：工具输出关键词查找验证

日期：2026-09-09。范围：ADR-0054。C/D/E 不在本次授权内；未提交或推送。

## 实现与事实源

新增默认 PURE_READ `search_tool_output`，与目录、reader 借用同一 SessionService 和
`resolve_tool_output`。只搜索已定位的当前 Session retained content/canonical data；原文仍是
原 Effect outcome，不加索引、数据库或 Shell grep 副作用。Session 10 / Context 9 / SQLite 2 不变。

query 为字面量，默认大小写敏感。默认只返回命中行（context_lines=0），显式请求才给附近行；
命中与 before/after context 分开。read_action 提供同源位置和页预算 count，方便片段不足时展开。
整页 JSON 有界，长行缩小片段仍保留匹配，命中翻页不跳过重复项；无命中不推导语义不存在。
预览按真实 registry 提供搜索入口，有搜索工具时不再用 offset=0 的读取动作引导逐页扫描。

## 定向测试与门禁

最终具名集合：**252 passed / 1 skipped，85.11 秒**。跳过仅为 Windows 无目录符号链接权限。
日志：`.pytest-tmp-codex/tool-output-search-tests-final-02.txt`。

```powershell
python -m pytest tests/test_retained_tool_output.py tests/test_tools.py tests/test_tool_runtime_failures.py tests/test_tool_middleware.py tests/test_recovery.py tests/test_compaction.py tests/test_surface_and_invariants.py tests/test_active_request.py tests/test_context_runtime.py tests/test_plugin_runtime.py tests/test_history_runtime.py tests/test_reference_orchestration.py tests/test_reference_read_actions.py tests/test_reference_retention.py tests/test_v07_d0_architecture.py tests/test_sqlite_event_store_architecture.py tests/test_product_architecture.py tests/test_context_request_protocol.py tests/test_tui_optional.py --tb=short -rs -o addopts='' -q
```

覆盖真实 Shell/SQLite、压缩后重启搜索和展开、中文/emoji/JSON 转义、字面正则符号、大小写、
长行、重复命中及按预算分页、无命中、content/data、错 Session/digest/workspace 拒绝、输入失败、
确定性重复取消。B 的持久化失败/恢复测试继续通过，结果与模型请求重放及来源不变量一致。
搜索页不产生递归 retained 引用，执行计数证明未重跑原进程。

反向验证两项：移除字面转义，content/data 测试均因匹配丢失失败；恢复默认携带附近行，真实
Shell→压缩→重启→搜索测试因泄出非请求上下文失败。均逐字节恢复源码，再运行最终绿色集合。
日志分别为 `.pytest-tmp-codex/tool-output-search-reverse.txt` 和
`.pytest-tmp-codex/tool-output-search-default-reverse.txt`。

初轮相邻检查有一项默认工具排序预期错误：registry 的 search_text 排在 search_tool_output 前。
已修正显式工具名单，未放宽生产注册或测试条件。AgentRuntime 的架构摘要只随新增默认工具装配
更新，并在测试内记录 ADR-0054 理由，其余三项保护摘要不变。

`compileall -q src tests`、修改范围 Ruff、全仓仅收集 **3447 项/140 文件**通过。
两份上下文章节 0–20 对应；8 份文档的 552 个相对链接存在、45 个 Mermaid 块闭合，流程人工核对；
差异检查通过，生产文件未发现本轮案例词，文档和证据没有匹配真实 Key 形状的文本。初次密钥扫描
误匹配 task-* 名称，改为带词边界的检查后通过；不回显任何可疑凭据。
未运行全量测试、L2–L4、Wheel、安装或包索引访问。真实模型门禁单独显式运行。

## 真实模型验收

使用用户授权的现有配置加载凭据，通过正式 Provider、Runtime、Tools、SQLite 和压缩/恢复主线。
不输出或保存密钥，不改用户 launch profile、工作区或会话。所有试验位于新建隔离目录。

```powershell
python tests/live_tool_outputs/run.py --profile <已有启动配置文件> --output <新的隔离目录> --keyword-search
```

占位路径和以下日志案例均是验收输入，不是生产默认。程序运行时生成 260 个多行随机记录，
记录标题与随机字段之间隔着采样行；脚本源码不含答案。模型的问题不提工具名、分页或内部 API。

每种进程退出码 0/7 分别验证：自然查询某条记录、M3 后关闭再打开查询另一条记录、两个重复标记、
明确无命中和新话题。脚本比较实际原文随机字段，断言搜索发生在读取前、真实展开与目录查询，
原程序仅执行一次，请求重放和完整来源不变量通过。无命中同时检查空搜索结果及明确否定回答，
支持真实模型的中文或英文表达，不以指定输出语言替代语义判断。

最终多行真实验收 **run-08 completed=true**：qwen-plus、两种退出码、**10/10 回合通过**；
8 次搜索、9 次原文读取，两次原程序执行各恰好一次。每个首次/重启目标查询均先搜索再读；
重复标记查询各读两处原文，无命中回合只搜索，新话题均回答 42。实际源码随机字段均比对正确，
请求快照重放和完整来源不变量通过。日志 `.pytest-tmp-codex/tool-output-search-live-08.txt`，
脱敏报告 [run-08](validation-data/tool-output-search/run-08.json)。

仍有可见额外开销：恢复阶段共先失败读取 History 4 次（exit 0 三次、exit 7 一次），随后转到目录。
44 次正式 Attempt 中 37 次提供已知 usage，合计 288,465 tokens；7 次 TLS EOF Provider 失败无 usage，
不能按零成本计算。前八轮累计 96 次 Attempt、89 次已知 usage 合计 659,263 tokens、7 次未知。
网络失败类别以事件汇总为准，不改变成功/失败语义。

单行随机 JSON 记录补测 **completed=true、6/6 回合通过**：4 次搜索、**0 次 read_tool_output**，
直接根据命中行回答；两次原程序各只执行一次，重启时各读一次 History 再查输出目录。
18 次 Attempt 全部有 usage，合计 81,807 tokens。见
[单行记录报告](validation-data/tool-output-search/run-single-line-01.json)。

最终两组共 **16 个真实回合、12 次搜索、9 次按需展开**；四个进程均只执行一次。
两组成功验收合计 62 次 Attempt、55 次已知 usage 为 370,272 tokens、7 次未知。
包含全部探索失败的九次运行合计 114 次 Attempt、107 次已知 usage 为 741,070 tokens、7 次未知。
这证明了当前模型/样本上的实际行为，不宣称其他模型或任意文本均能完全正确导航。

## 保留的失败与修正

脱敏逐轮报告与从隔离 SQLite 读取的用量/调用统计见
[证据汇总](validation-data/tool-output-search/runs-summary.json)。原 SQLite 和终端日志保留在
`.pytest-tmp-codex/tool-output-search-live-*`，未把工作数据库提交到文档。

| 运行 | 已观察结果 | 分类与处理 |
|---|---|---|
| 01 | 答对，但先读第 0 页再搜索 | 导航未满足先搜索验收；补充通用说明 |
| 02–03 | 把命中前一条记录字段归给目标记录 | 真实错误答案，单加提示无效；分开命中行和附近行 |
| 04 | 答对，但先读三页再搜索 | 移除搜索可用时预览的默认从头读取引导 |
| 05 | 搜索后自行只读 200 字符，没读到答案仍误配前文 | 展开动作补全来自当前预算的 count |
| 06 | 首次搜索展开通过；重启后仍误用附近行 | 默认改为普通 grep 的只返回命中行；附近行显式请求 |
| 07 | 前三项答案与调用通过，无命中也正确回答英文 | 脚本只识别中文否定导致误判；保留原失败，修正语言覆盖后全旅程重跑 |
| 08 | 十个回合、来源核对及执行计数全部通过 | 含 7 次 TLS EOF 重试与 History 绕路，开销照实保留 |
| single-line-01 | 六个回合通过，4 次搜索、0 次展开 | 命中行足够时直接回答，两种退出码各只执行一次 |

已知边界：单来源字面检索，不搜索 inline 小结果或提供语义/跨输出召回；原文仍整体加载并占磁盘。
显式附近行仍要求模型核对记录归属，不能保证任意模型每次正确。History 导航的额外绕路和外部
网络失败单独记录，不将其隐藏为成功，也不把未证明由改动导致的网络错误列作源码问题。

## 文档同步

正式版先于通俗版同步 1、3、4、9、12、13、15、16、17 节，包含 owner、三工具流程、默认参数、
同源位置、搜索/展开动作、重放、验证和边界。两版主章节仍为 0–20；Mermaid 流程加入搜索路径。
计划新增 B+ 位于 B 与 C 之间；ADR-0053 保留原决策，新增 ADR-0054 记录本轮选择。
