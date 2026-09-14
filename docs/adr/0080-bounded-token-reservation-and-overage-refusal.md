# ADR-0080：预算预留的可证明上界与超支拒绝

状态：接受。日期：2026-09-14。修正 Budget 预留与结算的组合行为，并为 Product Profile 增加一个必填声明。

## 原因

三轮真实运行暴露出一条完整缺陷链，每一环都有源码与事件佐证：

1. `BudgetedLlmRuntime._bounded_request` 在**没有 token 计数器**时，把**账户全部剩余**作为单次调用的预留额；
2. 生产里**没有任何路径提供计数器**——`BudgetEnforcement` 只在 `product/runtime.py` 与
   `evaluation/model_service.py` 两处构造，均不传 `token_counter`，且 `src/traceh/` 内
   **没有任何类实现 `TokenCounter` 协议**（`RequestTokenMeter` 当时只有 `count_message` / `measure`）。
   该有界预留分支因此是**只有测试会走的死代码**；
3. `llm/openai_compatible.py` 构造的超时 `ProviderFailure` **不带 usage**（同文件的 DNS 失败带
   `Usage(0, 0, EXACT)`）；
4. 失败且无 usage 时按**预留全额**计费；
5. 账户归零后，宿主自己声明的重试策略（TIMEOUT 可重试、最多 3 次）在**准入处**被
   `BudgetExhaustedError` 拒绝，异常从 `runtime/agent_loop.py:285` 逃出并使整个 Turn 失败。

实测：`budget/usage-reserved tokens=4653578` → `budget/usage-settled tokens=4653578 quality=unknown`
→ `runtime/error BudgetExhaustedError`，step_id 与超时尝试相同。**一次零返回的调用吃掉了 465 万 token
的授权，并使重试在数学上不可能。**

另有一处相关行为：`_usage_settlement` 在 `total > reserved` 时按预留额封顶计费，即**真实超支被静默
丢出账本**。两条规则单独看都是安全方向（数不出就预留全部＝不超支；未知用量就全额计费＝不少计），
**问题出在二者叠加**，而现有测试只分别覆盖了每一条，从未覆盖其组合。

## 决定

不变量：**预留额必须是这次调用的可证明上界，而不是账户余额；真实超支不得静默离开账本。**

1. **可数路径。** Product Profile 新增必填键 `token_estimate`：
   `{"encoding": <分词器名>, "margin_percent": <0–100>}`，或显式 `null`。宿主据此构造
   `CanonicalTokenCounter` 并连同余量交给 `BudgetEnforcement`。预留额为
   `padded_input + output_limit`，余量**只加在计数出来的输入侧**——输出上限是告诉 Provider 的精确值，
   不需要 padding；padding 后的输入同时压缩可买的输出，预留永不超出账户。
2. **不可数路径。** 没有计数器时，预留额改为
   `min(剩余, 规范化请求的 UTF-8 字节数 + 输出上限)`。字节级 BPE 的每个 token 至少映射一个字节，
   因此该值是 token 数的**数学上界**：既不会少计费，也不再吞掉整个账户。以 `remaining` 封顶，
   保证准入宽松度与此前完全一致。
3. **超支拒绝，并区分两种原因。** `_usage_settlement` 在 `total > reserved` 时抛 `BudgetUsageOverageError`
   （`code = "budget-usage-overage"`，携带 reported/reserved），不再静默封顶。成功分支既有的
   "仍按预留额结算 → 再抛出"路径直接承接，账户保持一致；失败分支原本**缺少**该保护，本次补上并
   保守结算，**不遮蔽 Provider 主错误**。
4. **协议。** `token_estimate` 为必填键，`profile_version` 6 → 7。缺该键的旧文档被明确拒绝
   （共享设置解析器点名 `token_estimate`），不静默补默认值。
5. **计数只有一套实现。** 从 `RequestTokenMeter` 中抽出 `CanonicalTokenCounter`，输入计量与预算预留
   共用同一个规范化计数原语；计数器本身不携带任何 window 策略，因此"数 token"不需要宿主编造
   `window_tokens`。

> **2026-09-14 细化（实测后）**：真实运行显示这条规则会把**账户耗尽**也报成溢出。第 5 轮 single 臂最后几次预留为 504,658 → 353,840 → 201,730 → **48,300**——预留额被**剩余余额**卡住，而该次调用真实用量约 153,430，于是事后触发溢出错误。「token ≤ 字节」这一上界前提**并未被推翻**，卡住预留的是余额而不是上界。
>
> 因此结算处增加一条区分：预留额**拿走了账户全部可用额度**时，报 `BudgetExhaustedError("max_tokens")`（账户本就付不起这次调用）；预留额**未被余额卡住**而用量仍然超出时，才是 `BudgetUsageOverageError`（宿主自己的估算错了）。判断从同一账本读取，不新增状态。
>
> 另一个被试错后放弃的改法：把字节上界用作**准入闸门**（装不下就拒绝）。它会按 3–10 倍拒掉付得起的调用——小预算夹具的请求 JSON 有数百字节，`max_tokens=10` 会被直接拒绝。**字节上界适合当预留大小，不适合当准入闸门。**

## 被否决的替代方案

- **仅接入计数器，不改不可数路径。** 分词器是可选依赖，TUI 生成的默认宿主与仓库自带 benchmark 都会
  声明 `null`，缺陷将原样保留在默认配置里。
- **不加计数器，只用字节上界。** 可修复缺陷且无需协议变更，但预留额对英文约为真实 token 的 3–4 倍，
  预算临界处会过早拒绝；且成本口径不是 token，与宿主其余部分不一致。
- **预留额取一个固定小上限。** 没有计数也没有上界证明，真实用量超出预留时会按预留额封顶，
  即**少计费**，破坏账本作为授权事实源的意义。
- **超支时继续静默封顶。** 保留今天的行为，但真实超支永远不可见。

## 边界

计数路径依赖 tiktoken，其编码对非 OpenAI 模型是**估算**（`quality: "estimated"`），这正是 `margin_percent`
存在的原因；余量由宿主显式声明，不从模型名推断。成功调用的结算仍以 Provider 报告的精确用量为准，
本 ADR 不改变它。超支拒绝对**所有**路径生效，因此此前"超支后静默继续"的运行现在会失败——这是有意的
行为变更，不是纯缺陷修复。本轮未用真实模型验证，Session/Promotion/事件协议版本不变。
