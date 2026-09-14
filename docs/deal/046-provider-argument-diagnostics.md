# Provider 参数失败诊断

2026-09-13。承接 WC-1G 的 `provider-tool-arguments-invalid`。本轮补可持久化的诊断类别，不改解析接受范围、不重试真实任务。

## 根因与实现

原 `_parse_tool_arguments` 将原始类型错误、非法 JSON 和 JSON 顶层不是对象统一成一个错误码，原日志无法区分。Provider 已有明确的无正文错误边界，因此不把原参数、HTTP body/header、认证信息或异常文本写入生产日志。

现在复用原 ProviderFailure → 原 Runtime → model/attempt-end.failure_code：

| 后缀（共同前缀 provider-tool-arguments-） | 解析器确认的现象 |
|---|---|
| type-invalid | 原参数既不是对象，也不是 JSON 字符串 |
| not-object | JSON 能解析，但顶层不是对象 |
| json-comma-expected / json-colon-expected | 解析器期待逗号 / 冒号 |
| json-key-expected / json-value-expected | 解析器期待带双引号的键 / JSON 值 |
| json-string-unclosed / json-extra-data | 字符串未闭合 / JSON 之后还有内容 |
| json-control-character | 字符串出现未转义控制字符 |
| json-escape-invalid / json-unicode-escape-invalid | 非法转义 / Unicode 转义 |
| json-nonfinite | JSON 不允许的非有限数字 |
| json-syntax-invalid | 未细分的 JSON 语法错误 |

仅将固定解析器类别映射成固定代码，绝不拼接原异常文本；未知类别有固定兜底。代码解释的是解析器观察，不证明上游生成、截断或网络原因。例如三引号也可能触发“期待逗号”，不能据此断言就是少写一个逗号。

原成功路径、有限三引号规范化、权限、Budget、取消、失败收敛和 protocol 不重试保持不变；没有新事件类型、协议版本、存储或 AgentLoop 逻辑。显式 `provider_argument_probe` 沿用原工具并识别新错误码族，没有启动它的真实调用。

## 验证

- 81 项不同定向/相邻用例通过，零跳过：Provider、参数探针、Runtime 重试/取消及 TUI 错误展示。
- 新增 13 种输入通过本地 HTTP → 真实 OpenAICompatibleProvider → 原 Runtime，检查原失败事件与请求重放、没有 Tool 执行、每次只调用一次、不泄露响应哨兵内容。
- 已有成功解析与合法三引号用例仍通过；已有失败、取消、额度结算用例仍通过。探针原粗码断言调整为新精确码后复测通过。
- 隔离源码回退到粗粒度错误后，缺逗号用例经实际 HTTP/Runtime 调用，因错误码仍是 invalid 而按预期失败；正常实现的 13 项复测通过。
- compileall、4138 项 collect-only、5 个修改/新增 Python 文件 Ruff、文档链接/围栏/主章节对应与 diff 检查。通用生产文件反示例扫描通过，测试字符串仅为显式夹具。

两份上下文同步第 8、15 节；通俗版同时补齐第 14.3 节已有收尾模块的职责行。未运行全量、L2–L4、Wheel，未调用外部模型、未提交发布。

## 尚未证明

旧 WC-1G 没保存原始失败参数，细分诊断不能回填旧事件，也不能解释那次究竟是哪一种错误。本轮只证明未来失败能够分类留证，不证明真实收尾质量改善。需要真实原文观察时应另行冻结受控诊断调用，复用已有显式取证脚本，不把原文日志偷偷变成生产默认。
