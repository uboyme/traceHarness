# v0.9 Stop C / C4 Provider 工具参数边界

- 状态：C4 受控调查与限定确认完成；没有确认的 Adapter 源码缺陷，历史参数错误的具体字节原因仍未知。
- 范围：原 `provider-tool-arguments-invalid`；不放宽 parser，不执行返回的 Tool，不恢复旧 Session。
- 真实 Provider 已获授权；全量、L2–L4、安装、Wheel 与提交发布继续禁止。

## 1. 已核实的历史证据

原 DeepSeek 导航运行在 Session `40ed4d95-eb71-4b3e-941a-f1d3614fa068` 的 seq 22 记录
`provider-tool-arguments-invalid`，类别 `protocol`，原请求 snapshot 为 seq 20，Turn 最终失败。
原 Adapter 在尝试将 `function.arguments` 转为 JSON object 时拒绝；历史导出没有保存 HTTP body，
因此不能区分当时是截断、非法 JSON、非 object，还是超出既有有限 dialect。

当前 Adapter 字节摘要与该历史运行 manifest 完全一致：
`4557857f9348e2b3fcce141fe41dd1dbc9f952d5c2c27698c5119af0e0b79c13`。
原请求的 Stream、Attempt 引用、Step/Turn 与 dispatch fingerprint 已对齐；当前 `ModelRequest`
逐字段反序列化后完全一致，指纹也相同。模型为原 `deepseek-v4-pro-0813`、temperature 0、
max_output_tokens 16384，内容是合成传感器维护资料问题，未加入答案、改写提示或替换模型。

## 2. 受控重放与现行合同

[显式诊断](../tests/provider_argument_probe/README.md) 在调用前固定 24 次独立同请求调用、每次
300 秒超时；使用当前显式 Provider 配置，无 fallback、隐藏 retry 或失败替换。只通过原 Provider
读取 HTTP response body，再独立区分 JSON/object 解析、Adapter 输出与原 Tool JSON Schema。
返回的工具不执行，JSON Schema 通过不代表所指资料身份或权限有效。

历史错误与当前连接中断分别记录。没有 body 的尝试不能算成参数错误，也不能从未知用量推导零用量。
原 Provider 现有严格 JSON 与有限三引号规则保持原样；没有增加 parser 修复、猜 ID 或类型 coercion。

四条诊断预检通过（2.04 秒）：原公开 Provider 经本地真实 HTTP 接受合法参数、保留合法 JSON 的
类型错误供 Tool Schema 拒绝、拒绝非法 JSON，并在传输失败时保持 body 不可用。原始响应逐字节保留，
请求 payload 摘要相符，服务线程关闭；认证值不进入记录。Provider、重试、工具失败与诊断四个具名
文件共 53 项通过（16.26 秒）；修改范围 Ruff 通过。

## 3. 真实结果与离线核算

24 次固定尝试完整保留：21 次解析成功，1 次 `provider-disconnected`、2 次 `provider-tls-eof`。
三次网络失败均未取得 body，用量未知；它们仍在 24 次的分母中，未替换或重试。
后续收到的 21 份 body 中共有 18 个 `request_skill_reference` 调用，参数均为合法 JSON object，
与原 Adapter 输出逐字段一致且通过原 JSON Schema。另 3 份响应没有 Tool Call；不把它们算成
资料导航成功，也不在本轮执行返回的工具。未再出现 `provider-tool-arguments-invalid`。

为区分基本连通性，另冻结一次同模型、无工具、32-token 上限的最小文本控制；成功收到并解析 body。
它不替换任何原尝试。原 24 次已知 wire usage 为 67,024 tokens，3 次未知；控制另 101 tokens。
这里的 wire usage 来自所保存 HTTP body，不冒充未执行的 Session Attempt 或 Tool 成功证据。

独立核算校验原 manifest、源码摘要、请求指纹、24 次 payload 摘要及每个 body 摘要，并将 22 份
实际响应经原 Provider 做离线字节重放，逐字段核对分类、参数和用量，全部一致。原 Adapter 与历史
运行的字节摘要也相同。完整尝试与摘要见
[C4 数据](validation-data/stop-c-c4-provider-arguments-2026-09-08.json)。

结论：这次没有复现历史参数错误，没有确认的 Adapter 解析缺陷，故不增加修复、放宽规则或猜参数。
历史缺失 HTTP body，仍无法追认其具体语法形态；当前三次网络失败也没有证据表明由本地源码造成。
这些属于保留的证据边界，不能写成“历史失败已修好”或“24 次全部成功”。原失败仍按 protocol 拒绝，
Tool Schema 与资源权限仍由各自原 owner 校验；生产源码及协议不变。

最终核对 244 个生产模块与 C2 接入时字节相同；本轮 Provider/诊断冻结源码无漂移。compileall、
修改范围 Ruff、diff-check、新文件空白/秘密形态和反硬编码检查通过；3300 项仅收集，未执行全量。
正式版先更新第 2、7.10、8、15 节，再同步通俗版；章节、链接与围栏检查通过。未跑 L2–L4、
Wheel/安装或发布级门禁，未修改 Git 历史。C1–C4 的限定工作已收口，下一步 C5 独立审查。
