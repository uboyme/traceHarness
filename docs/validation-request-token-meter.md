# E0：完整请求 Token 计量验证

日期：2026-09-09。范围见 [ADR-0056](adr/0056-complete-request-token-metering.md)；分层顺序为
A/B/B+/C → E0 → D → E。本阶段完成估算、触发/拒绝和实际用量显示，不包含模型摘要或轮内自适应取舍。

## 确定性公开路径

`tests/test_request_token_meter.py` 的 15 项验证覆盖：中文/代码与六部分完整计量，实际 Runtime/SQLite
计量和 usage 分离、重启、源重建、伪造分项拒绝，首请求超限不产生 Snapshot/Attempt/Provider 调用，
写前/写后错误、重复取消、两个并发头变化窗口、模型绑定/不合法策略、CLI profile、Timeline 安全文案、
当前工具读回使下一 Step 超限时保留调用/回复完整性，以及大字节阈值下由 token 压力触发旧结果折叠。
界面 reader/presentation 核对输入估算、实际输入/输出和估算标签，不把模拟 usage 当真实模型统计。

F2 公共 Textual pilot 打开“Token 预算”，只填编码时启动被拒；补齐窗口/预留/余量/百分比后可以保存、
读取并应用，未创建 Store 或发请求。已有配置取消、文件写入失败和应用收尾检查继续通过。

- 29 个明确指定的 owner/相邻测试文件初次回归：858 passed、1 skipped、1 failed，87.35 秒。
  失败是 dispose 用例的默认工具名单仍停在 B/B+ 之前的五个工具；更新为当前八个默认工具，未修改生产清理逻辑。
- 修正后 `test_request_token_meter.py`、`test_runtime_dispose.py`、`test_tui_settings.py`、
  `test_product_architecture.py`：72 passed，25.51 秒。包含上述失败项和新增 F2 表单交互。
- 最后增加展示断言，计量 owner 与 `test_tui_optional.py`：24 passed，5.67 秒。
- 初期另发现 CLI recover 夹具在冻结 Context 前没有当前 user/message，按既有问题锚点合同补齐真实消息并
  更新对应断言；未放宽请求重建规则。F2 返回未解析启动输入，测试按实际 CLI 配置解析后再验证策略。

相邻回归覆盖 Context/History/Surface/Recovery、Runtime factory/dispose/e2e、模型 admission/retry、严格
费用 Budget、CLI 启动/恢复/配置、TUI reader/presentation 和 Plugin composition。没有运行完整 pytest 或 L2。
上述数量属于不同范围与迭代，不能相加宣称互不重复的测试总数。

## 反向验证

临时替换生产逻辑，逐项证实预期失败，随后逐字节恢复并重跑 owner：

1. 去掉最终 over_limit 拒绝：公开 Runtime 请求被放行，超限拒绝用例失败。
2. 把计量 CAS 从 built.source_seq 改成随后读取的 head：并发事件之后错误计量记录落盘，期望零记录变为一条。
   原模型 permit 仍拒绝调度，不能把这条错误持久证据误说成已调用模型；新 CAS 在写入前就拒绝。
3. 漏算工具定义：中文与代码请求的分项完整性断言同时失败。

## 真实模型与真实工具

沿用用户授权的既有连接加载方式，在隔离临时目录运行普通 Runtime、真实 Shell、SQLite 和
`qwen-plus`；未修改用户 profile、会话或工作区，未写入/显示密钥。

入口为 `tests/live_tool_outputs/run.py`，命令形态如下（路径是占位示例）：

```powershell
python tests/live_tool_outputs/run.py --profile "<existing-profile.json>" --output "<new-isolated-dir>" --keyword-search --fold-tools --token-meter
```

每个进程现场生成 260 条含随机设备标签、复核码的多行记录，两组退出码分别为 0 和 7。模型查找指定
记录，补一轮近期对话，然后在 token 触发折叠、重启之后分别查另一个记录；继续验证重复关键词、
不存在的记录和“23+19”新问题。仅靠提示无法猜出现场随机值；原脚本每组执行一次，读回不重跑。

最终 [run-03](validation-data/request-token-meter/run-03.json)：

| 指标 | 结果 |
|---|---:|
| 对话回合 | 14/14 通过 |
| 服务调用 | 37 succeeded；无失败重试 |
| 已知实际总 token（输入＋输出） | 456929 |
| 缺失 usage | 0 |
| 关键词查找 / 原文展开 | 10 / 10 |
| 两组原脚本执行次数 | 各 1 次 |
| 最终超限拒绝 | 0（拒绝由确定性用例和探索轮验证） |

测试显式选择 `cl100k_base`，并给出测试窗口/1024 输出预留/1024 安全余量；折叠阶段窗口由上一请求
实测估算派生，提前比例为 80%。这些是隔离实验参数，不是生产默认编码或该服务真实窗口声明。

## 估算与真实 usage 逐请求对照

从同一 SQLite 原始事件按 Step/composed fingerprint 绑定计量和 request snapshot，再用
attempt-start.request_snapshot_seq 连接 attempt-end.usage，保留每次输入、输出、误差及六项估算。
原始对照在 [runs-summary](validation-data/request-token-meter/runs-summary.json)。误差定义为
`(本地估算 − 实际输入) / 实际输入`，MAPE 为绝对百分比误差的平均值。

最终 37 对观测：平均绝对百分比误差 **19.6418%**，最小 **−11.843%**，最大 **+26.7299%**。
它证明可以测量、核对与暴露误差，不能证明本地编码精确匹配 Qwen，也不构成所有语言或模型的误差上界。
当前不按这一次模型/样例写死修正系数；安全余量明确配置，估算不能作为实际账单。

最后用最新源码重新检查两个账本：exit-0 的 263 条 Session 事件/19 条计量、exit-7 的 250 条事件/
18 条计量均通过完整不变量与精确请求重建，前后事件相等。见 [最终重放证据](validation-data/request-token-meter/replay-final.json)。

## 探索失败与成本

- [run-01](validation-data/request-token-meter/run-01.json)：只在硬上限触发的早期方案，折叠后能够搜索，
  但继续读原文后估算 12244 超过输入上限 11734，被正确拒绝。暴露了工具后继空间不足，所以最终设计分开提前线与硬拒绝线。
- [run-02](validation-data/request-token-meter/run-02.json)：提前触发后三个回答均正确，测试脚本仍用旧的
  Turn 前消息数比较首 Step 折叠，漏算刚入账的当前问题。改为比较真实 fold 事件前后同一截止点，未改变生产保留规则。
- 两次探索分别 8/9 次服务调用、53745/63963 个实际总 token；三轮合计 54 次、574637 个已知实际总 token。
  失败轮保留，未拼接成最终通过轮。探索中的早期计量形状是开发过程产物，不承诺跨算法迁移。

## 门禁与仍有的边界

`compileall src tests` 与修改范围 Ruff 通过；pytest collect-only 收集 3480 项（仅收集，未全量执行）。
两版章节、相对链接、Mermaid 闭合及证据 JSON 由收尾脚本逐项检查，git diff --check 检查空白错误。架构保护只更新 AgentLoop/AgentRuntime 的
受影响指纹并解释 ADR-0056；Supervisor/PluginManager 保护不变。可选依赖声明新增 tokens，无安装或 Wheel 构建。

未运行全量、L2–L4、发布候选、联网包索引或提交/推送。参考资料内部仍使用既有字节配额，最终一起计入
完整 token 估算；轮内自动取舍、语义摘要、跨模型精确计数和自动误差校准均未实现。文件/磁盘字节边界保留。

正式版与通俗版同步当前状态、目录/装配、事件/请求、工具与压缩、配置/显示、验证、边界和变更影响
（1/3/4/6/7/9/12/13/15/16/17，M3 对应章节）；另同步阶段计划、配置说明、README、CHANGELOG 与 ADR-0056。
