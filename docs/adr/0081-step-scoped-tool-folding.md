# ADR-0081（草案）：Step 粒度的工具结果折叠

状态：**草案，未决策、未实现，当前不采用**。日期：2026-09-14。扩展 ADR-0042 的 Surface 压缩与既有
`tool-fold`，不改写其决定。

> **2026-09-14 更新（规模修正）**：起草时把本方案估为"把已有机制下沉一个粒度"，**低估了**。
> 试做后发现持久化折叠事件的校验被钉死在 Turn 上：`validate_tool_fold`
> （`session/surface_replacement.py:525-530`）要求 `fold.cut_seq` 必须**恰好等于某个 `turn/end` 的
> seq**（`closed_turn_ends(prior)` 去掉 `kept_recent_turns` 之后的最后一个），否则
> `tool-fold-protected-turn`。因此 Step 粒度折叠不只是换个选择条件，而是**改 append-only 替换事件的
> 协议**（新的 cut 语义与 `kept_recent_steps` 字段）、改校验器、并连带改会话不变量重放。
> 量级不低于 ADR-0080。
>
> 据此**先不做本方案**，改为从输入侧降低进入上下文的量（`read_file` 结构模式）。若实测证明输入侧
> 不足以让主 Agent 上下文收敛，再回到本 ADR 并按上述真实规模评估。
>
> 试做期间为本方案准备的两处改动（内联结果也写出 `output_ref`、Step 粒度成员映射）因**当前没有任何
> 消费者**已全部撤回，撤回后 `git diff` 对相关文件为空。

## 原因

Product 任务在单次上下文装不下的语料上无法工作：主 Agent 的模型请求单调增长直至 Provider 失败，
期间**没有任何机制卸掉压力**。实测（第 3 轮 single 臂逐次请求的 exact input tokens）：

```
#3 3,343   #10 33,700   #15 61,347   #19 75,403   #27 89,856   #28 失败
```

全程上升，从未下降。仓库里有两套卸压机制，都无法在此生效，原因是三处相互独立的限制。

### 限制一：Product 未装配压缩

`src/traceh/product/` 中没有任何 compaction 引用，`RuntimeConfig.compaction` 取默认 `None`。
装配点只有 `cli/main.py:616`（chat/TUI 共用），且默认关闭。

### 限制二：读取类结果从不被保留，因而不可折叠

`tool_fold_plan`（`session/surface_replacement.py:485`）只选取 `"output_ref" in source.data` 的结果，
即**写入时被保留过**的那些。而 `prepare_tool_output` 按大小判定：小于 `max_tool_output_chars` 的内联，
**不带 `output_ref`**。`read_file` 每页上限 8000 字符（`tools/builtins/file_page.py:10-11`），
`max_tool_output_chars` 默认 24000，因此**阅读产生的结果永远内联、永远不可折叠**。

### 限制三：折叠只发生在已闭合 Turn

`tool_fold_plan` 的文档串写明「Select one still-visible retained result in **a genuinely closed old
Turn**」，实现上要求 `entry.seq in membership`（闭合 Turn 成员）且 `membership[entry.seq] <= cut_seq`。
`_fold_plan` 的 `cut` 同样来自 `_cut_boundary`，即 `turn/end`。

而 Product 任务的**全部工作在一个 Turn 内**。实测第 3 轮：

| 臂 | Turn 数 | 各 Turn 的 Step / 工具调用 |
|---|---|---|
| single | 3 | 1/0、1/0、**20/79** |
| multi | 3 | 1/0、1/0、**38/126** |

前两个 Turn 是聊天与提议握手，零工具调用。因此**按构造，没有任何结果会成为可折叠对象**。

> 三条合起来：即便给 Product 接上压缩，也仍然什么都折不掉。这解释了为什么"接上压缩"不是本问题的解。

## 决定（待批准）

不变量：**模型在第 N 步实际看到过什么，必须始终可从 append-only 事件推导；折叠只改变呈现，不改变事实。**

### 1. `output_ref` 始终写出，但不复制正文

`prepare_tool_output` 对内联结果也计算并写出 `output_ref`（payload 的 digest 与尺寸），
**presentation 保持今天的内联正文不变**，且**不**额外写 `retained_output`——原文已经在
`tool/result.content` 与 `effect/outcome.content` 里，重复存储会让事件日志中同一份正文出现两次。

`resolve_tool_output` 相应增加一条：`retained_output` 缺失时，从同一 outcome 的 `content`/`data`/
`evidence` 重建 payload，并以 `output_ref.digest` 校验。既有的 intent ↔ outcome ↔ result ↔ call
身份链**一条不放松**。

效果：任何工具结果**日后都可折叠**，而写入时的呈现与存储成本都不变。

### 2. 折叠资格从"闭合 Turn"下沉到"闭合 Step"

`tool_fold_plan` 的成员判定改为按 Step：一个结果可折叠，当且仅当它所属的 Step 已经结束，
且不在最近保留的 K 个 Step 内。

这条之所以安全，是因为**折叠本身已经保证了分组完整性**：`folded_tool_message` 只替换结果正文、
保留原 assistant 调用，`tool_fold_plan` 另行校验 call 与 result 属于同一分组
（`membership.get(call_seq) != membership[entry.seq]` 即报错）。ADR-0042 的"闭合前缀"规则是为了
**摘要式前缀替换**不切开 Step/Tool 组；对逐条折叠而言，该保护由折叠自身提供，Turn 边界不是必要条件。

**摘要式前缀替换仍然只作用于闭合 Turn，本 ADR 不改它。**

### 3. Product 装配

Product 角色授予 `read_tool_output` / `list_tool_outputs` / `search_tool_output`，
并接入折叠。触发与保留量由 Profile 显式声明，不设隐藏默认。

### 4. 触发与保留量

待定，三选一或组合：按已折叠后的会话字节、按最近 K 个 Step、按 RequestBuilder 的完整请求 token 压力。
**必须显式声明**，缺失即不启用。

## 被否决的替代方案

- **只给 Product 授予读取工具**。读过的正文仍全部内联堆积，一点不省。
- **调低 `max_tool_output_chars` 让阅读结果一律保留**。模型再也看不到任何文件内容，每页都要额外一次
  `read_tool_output`，调用数翻倍。
- **在构建请求时直接替换旧结果（不落事件）**。会让"模型实际看到的"与"事件记录的"产生不可见落差，
  违反本仓库的核心性质。
- **一律写出 `retained_output`**。可行但让同一份正文在事件日志中出现两次。
- **给 Product 接上现有压缩即可**。见限制二、三：接了也折不掉任何东西。

## 退出判据

重跑大项目实验，**主方上下文出现平台期**（不再单调上升），且仍能写出通过固定检查的交付物。
基线现成：`3,343 → 33,700 → 75,403 → 89,856 → 崩`。

若做完之后仍单调上升，则本方案错误，如实记录，不改题重试。

## 边界与风险

折叠改变模型可见内容，因此可能改变模型行为——它必须学会用 `read_tool_output` 取回被折叠的证据。
这是能力问题，不是正确性问题，但会影响实验读数，必须与收益分开陈述。
本 ADR 不改 Session/Product/Promotion 协议版本，不改摘要式前缀替换，不改审批与验证边界；
被折叠内容必须仍能解析到同一份不可变原文，这是硬要求，由第 1 条的 digest 校验保证。
