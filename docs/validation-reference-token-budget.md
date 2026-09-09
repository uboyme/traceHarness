# E2：参考 token 准入验证

2026-09-09。边界与决策见 [ADR-0059](adr/0059-reference-token-admission.md)。所有模型例子、额度及项目身份都是显式测试夹具，不是默认值。

## 1. 实现与定向门禁

原 RequestBuilder 先以字节候选检查维护压力，再由原 ContextInputService 按剩余 token 完整准入。
纯派生 context_tokens 重建固定请求；reader 校验冻结来源，不能伪造余量或复活未交付的保留授权。
当前 Product/问题/工具组不切割，最终 E0 硬门禁不变。

`test_reference_token_budget.py` 13 项覆盖中英文额度、固定来源伪造、三种正文/导航、未批准或撤销隔离、
固定 Product/当前工具/问题保护与副作用一次；retention 增加 token 模式的新阅读优先及实际逐出。
最终 14 个 owner/相邻文件 **313 passed in 164.05s**，不是全量；中间六文件 82 passed。
compileall、8 处范围 Ruff、全仓仅收集 **3523 tests** 通过。反向证据见 [reverse.json](validation-data/reference-token-budget/reverse.json)：
临时删除正文 token 门禁，正文错误进入；临时删除固定来源证明，伪造计数未被拒绝。保护已恢复再验证。

## 2. 真实 Provider

入口 `tests/live_reference_token_budget/run.py`，仅通过既有授权配置加载器，隔离 SQLite/Workspace，未修改用户数据或输出密钥。
测试 Skill、已批准 Memory、压缩 History 的真实阅读：宽窗 40000、窄窗 11000，预留输出 2048、边距 1024，
字节上限显式放宽以到达 token owner。正文尾部使用随机证据，模型必须实际阅读；窄额度不能猜证据。
History 建档也使用真实 Provider；原文不塞进提问或摘要。检查实际 Tool、请求正文、排除原因、答案与账本。

| 尝试 | 结果 | 调用 | 输入 token | 输出 token |
|---|---|---:|---:|---:|
| 01 | Skill 未读先拒绝，失败保留 | 1 | 3492 | 251 |
| 02 | 提示仍歧义，失败保留 | 1 | 3495 | 297 |
| 03 | Skill/Memory 四组通过，非完整最终验收 | 10 | 54378 | 1432 |
| 04 | History 宽通过、窄反复同页至步数耗尽，失败保留 | 12 | 95839 | 2547 |
| 05 | 全部六组通过，每组两步 | 14 | 110209 | 1973 |

最终六组加两次 History 建档共 **112182 token**；所有尝试共 **38 次、273913 token**，未隐去失败成本。
实际排除说明替代含糊提示后，模型不再重复申请未交付正文；提示本身的 token 已预留并计入。
六个最终 Session 用当前源码 fresh reader 精确重建请求、不变量零问题、事件未改写。
原始各次 report 保留在 `.pytest-tmp-codex/reference-token-live-01` 至 `05`；旧尝试使用中间渲染器，
不声称旧中间协议在最新渲染器下精确重放。可归档证据见 [runs.json](validation-data/reference-token-budget/runs.json)。

## 3. 限制与未运行门禁

token 是本地完整请求估算，实际 Provider usage 独立记录，不是隐藏模板精确计费。导航本身也可能装不下。
History 模型有时用英语或混合语言解释空间不足；不能承诺任何模型完全遵循提示。没有正文就不能编造答案。
当前 Turn 或固定内容太大仍会硬拒绝；分批摘要、Provider 超窗自动重试未实现。E3 随后提供用户可读提示。
未跑全量 pytest、L2–L4、Wheel/安装/发布门禁；未提交或推送。两份上下文同步当前能力、12.2 流程图、
模块职责和限制；文档链接/章节/代码块/秘密形态检查另见 doc-qa.json。
