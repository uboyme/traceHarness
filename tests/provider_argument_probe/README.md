# C4 Provider 参数诊断

这是显式开发诊断，不是产品日志开关、第二 Provider 或新的 Tool 执行入口。脚本不由 pytest 自动运行。
它从显式提供的历史事件导出中定位唯一 `provider-tool-arguments-` 码族失败（包含历史 `provider-tool-arguments-invalid`），核对原 Attempt、Step、
Turn、Stream、snapshot 引用和 dispatch fingerprint，再逐字段重现原 `ModelRequest`。
当前 Session 仍拒绝旧协议；读取合成历史请求做直接 Provider 诊断不恢复或改写旧 Session。

每次调用都是原 `OpenAICompatibleProvider.complete`，参数完全沿用冻结请求。显式冻结重复次数和
超时，没有自动重试、模型 fallback 或失败替换。HTTP 包装器只记录原 Provider 本来会读取的 response
body 和 request payload 摘要，原样返回字节并沿原生命周期关闭响应；不读取请求头或认证值。
只有原 `load_env_file` 加载显式 `.env`，报告不导出环境值。Tool 调用不会执行。

判定区分：

- HTTP body 未取得：属于网络/传输证据缺失，不能假装是参数解析错误；用量未知不能写成零。
- body 或 `function.arguments` 不是合法 JSON/object：记录原字节摘要和独立解析结果。
- 原 Provider 已解析：逐字段比较原始 JSON/object 与 ToolCall，另验证请求中原有 JSON Schema。
- 参数通过 JSON Schema 也不表示获得 Skill/Memory/History 资源权限；本诊断没有执行 Tool 的领域
  验证或实际读取，不把它计为真实引用旅程成功。

原 Provider 既有的有限三引号 dialect 若被接受，会单独标记；诊断不扩展此规则，不修补 JSON。
新的受控响应只能说明这次的实际行为，不能追认未保存 HTTP body 的历史失败根因。

运行前使仓库 `src`、`tests` 在 `PYTHONPATH` 中，设置 `PYTHONUTF8=1`。以下为显式开发示例；
事件文件必须是已经获准发送的合成测试请求，输出目录必须是新的。

```text
python tests/provider_argument_probe/probe.py --events <合成历史失败事件导出> --env-file <显式配置文件> --output <新证据目录> --repeats <预先固定次数> --timeout-seconds <明确超时>
```

每次的 `body.bin` 与判定保存在输出目录，最终 `report.json` 保留所有尝试。原 body 是取证材料，
仓库文档只汇总结果、模型身份和摘要，不复制凭据或请求头。
