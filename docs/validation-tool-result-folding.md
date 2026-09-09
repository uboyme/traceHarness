# 分层压缩 C：旧工具结果折叠验证

日期：2026-09-09。范围：[ADR-0055](adr/0055-old-tool-result-folding.md)。
本阶段完成，D/E 未开始；未提交、推送、跑全量、L2–L4 或安装门禁。

## 改动和证据

达到已配置的自动压缩阈值后，先收起旧 retained Tool result 的正文预览，保留原 assistant
调用及参数、tool reply 的 role/name/call ID 与原 output_ref。仍超阈值才做 M3 旧前缀摘要。
原始 Effect/Result 不改写；只追加同一 surface/replace 协议的 tool-fold 变体。

同一 CompactionService 负责选择、同源校验、CAS、写后对账与取消收敛。Context/History/TUI
分别识别摘要和工具折叠；后续摘要能递归找回原结果。失败会区分零写入、未知写入和前序折叠已提交。

## 定向验证

22 个 owner/相邻文件 **586 passed，112.62 秒**：

```text
test_tool_result_folding.py test_compaction.py test_retained_tool_output.py
test_surface_and_invariants.py test_history_reader.py test_history_runtime.py
test_history_requests.py test_history_tool.py test_context_input.py
test_context_runtime.py test_context_request_protocol.py test_tui_context_inspection.py
test_tui_presentation.py test_cli_timeline.py test_recovery.py
test_reference_retention.py test_reference_orchestration.py test_active_request.py
test_product_architecture.py test_v07_d0_architecture.py
test_sqlite_event_store_architecture.py test_tui_optional.py
```

运行方式：`python -m pytest <上列文件，各加 tests/ 前缀> --tb=short -o addopts='' -q`。
新增 17 项 C 用例覆盖：真实 Shell/SQLite exit 0/7、相邻短结果和调用参数保持、最近轮保留、
关闭不动、inline 跳过、原文缺失拒绝、重启搜索、M3 后 History 原叶恢复、请求快照逐字节重放、
写前失败、写后异常对账、确定性双取消、CAS 冲突、折叠后摘要及后续失败、伪造来源/消息/计量拒绝。
反向修改还原后重跑 C 文件：**17 passed，25.15 秒**。

- `python -m compileall -q src tests` 通过。
- 全仓仅收集：**3464 tests collected**，不执行全量。
- C 修改的 8 个生产文件、2 个测试/真实脚本文件 Ruff 通过。
- `git diff --check` 通过；终端只有已有工作区 LF/CRLF 提示。
- 文档 QA：6 份文档、两份上下文 0–20 编号章节对应、489 个相对链接存在、47 个 Mermaid
  代码块闭合，3 份证据 JSON 可解析且无秘密模式命中。两版同步 1/3/4/6/7/9/12/13/15/16/17
  与对应的 M3 说明（正式 20.39、通俗 20.33），修正旧“失败零改动”和目录分类描述。

## 真实模型验收

入口沿用 [原真实 runner](../tests/live_tool_outputs/run.py)，增加显式 `--fold-tools`：

```text
python tests/live_tool_outputs/run.py --profile <已有连接配置>
  --output <全新隔离目录> --keyword-search --fold-tools
```

使用用户已授权的现有连接，通过原配置加载器和凭据机制调用 **qwen-plus**。
不读取或输出原始密钥，不改用户会话。每组在新工作区真实执行一次诊断程序；260 条多行记录的
字段由子进程运行时随机生成，答案不在脚本源或问题里。分别正常退出和 exit 7。

先询问一条记录，增加一轮近期对话；测试宿主将显式 trigger 设置为当时 Surface 字节数减一，
再重开同一数据库。下一次普通 run_existing 在 Turn 前实际触发折叠，不用手工伪造事件。
随后自然提问另一条记录，再次关闭/重开数据库查第三条；另测重复关键词、无命中及换话题。
测试阈值从本次实际投影得来，是验收夹具，不是生产默认。

最终 [run-02](validation-data/tool-result-folding/run-02.json)：**14/14 回合通过**，
10 次真实关键词搜索、10 次附近阅读；原进程各执行一次，未重跑取答案。每轮都核对随机字段、
实际 Tool Result、完整请求重建和不变量。折叠前后逐项比较：只有原 shell reply 正文变化，
调用参数、消息数量、顺序、配对身份与最近对话相同。

| 场景 | 折叠前对话字节 | 折叠后对话字节 | 减少 | 回合 |
|---|---:|---:|---:|---:|
| exit 0 | 25,118 | 14,057 | 11,061（约 44%） | 7/7 |
| exit 7 | 25,182 | 14,121 | 11,061（约 44%） | 7/7 |

这是本组夹具的 canonical UTF-8 bytes，不是 token 节省率或任意会话效果保证。
最终 38 次 Provider attempt 均成功、报告 usage 合计 460,437 tokens；无未知 usage、无网络失败。
所有探索共 43 次已知 usage、496,222 tokens，按账本统计见
[运行汇总](validation-data/tool-result-folding/runs-summary.json)。

## 真实发现、修复和反向验证

[run-01](validation-data/tool-result-folding/run-01.json) 前两轮答对，下一轮在模型请求之前报
`HistoryReadError: history-block-unavailable`。原因是 Context 历史候选仍把任意 replacement
都当摘要目录根，而 History reader 已将 Tool fold 作为非目录中间节点。
修复位于原 Context 候选分类：只把 SurfaceReplacement 摘要变体作为根，Tool fold 不误列目录。
新增测试使用启用 History 的真实 Runtime 主线，所以能复现这个集成错误。

两项临时故障恢复检验完成后均按原字节恢复生产文件：

1. 恢复旧 Context 分类，两个 exit 场景均因相同 HistoryReadError 失败。
2. 移除折叠 payload 与原来源的完整重算比较，伪造 call ID 和字节数的两项用例均失败，
   证明检查器会失去应有拒绝能力；其余三项仍通过，未把夹具导入错误当有效反例。

首轮另有测试夹具漏填 HistoryReadPolicy 参数，已补齐；“极小预览一定无压缩收益”的夹具假设
也不成立，改用真实 inline 来源验证跳过边界。它们不伪装成产品缺陷，也不计入最终成功结果。

## 当前边界

C 只在 Turn 前处理旧 retained 结果；不缩减整批活动工具结果，不做全请求 token 预算、模型
语义摘要或 Provider 超限自动重试。小 inline 结果不折叠；自定义宿主须装配搜索/读取工具才可供
模型读回。完整原文仍占磁盘，读取和投影仍需扫描账本。真实用例成功不等于任意模型都能正确检索。
折叠与摘要分别提交，后续维护失败可留下前序合法折叠；原事实始终留存。
