# ADR-0056：完整请求 Token 计量前置阶段 E0

状态：接受。日期：2026-09-09。

## 决定与范围

用户批准把 E 的计量基础提前到 D 模型摘要之前。顺序改为 A/B/B+/C → E0 → D → E。
E0 为完整请求提供统一估算、触发与最终拒绝、可审计证据和界面显示；E 仍负责轮内自适应压缩、
按 token 精细选择参考资料和超限后的交互处理。文件、磁盘、传输和读取页仍使用物理字节/字符边界。

- 新 `llm/token_meter.py` 是唯一请求估算器。显式选择 tiktoken encoding，不用模型名称猜编码、
  窗口或平台。分别计量系统提示、Product 消息、对话、参考资料及当前问题回显、工具定义和可见
  请求封装；内部审计 metadata 不当成远端输入计量。canonical 部分 BPE token 求和是估算规则，
  不冒充远端 chat template 的精确分词。源内类似 special token 的文字按普通文本处理。
- `TokenBudgetPolicy` 明确 encoding、window_tokens、output_reserve_tokens、safety_margin_tokens；
  输入硬上限 = 窗口 − 输出预留 − 安全余量。trigger_percent 为可配置的提前触发比例，默认 80，
  范围 1–100；它乘的是输入硬上限，不是整个窗口。80 是当前公开策略默认，不是模型能力常量。
  初次真实验收证明等到硬上限才触发会给后续工具读取留下不足空间，因此分开触发线与拒绝线。
- Runtime 显式绑定 Provider/model 与计量策略，当前 Lease 换到其它模型时拒绝旧绑定；不把某个
  编码或窗口静默套到新模型。输出请求上限不得超过预留；未另给上限时使用预留作为请求上限。
- `RequestBuilder.prepare` 接管原 Context freeze、Context append、Composition append 与 build
  的既有顺序。AgentLoop 只调用准备入口，仍持有同一 Lease，Session dispatch permit 不变。
  计量启用时，首个 Step 先得到当前参考资料和用户问题的完整草稿；超过提前触发线且自动压缩开启，
  才由原 CompactionService 折叠旧闭合结果、必要时 M3 摘要，然后重新选参考资料并正式冻结。
  新引用目录可能变化，所以不能复用摘要前的 Context。活动 Turn 中仅旧闭合前缀可压缩，当前
  用户消息、Step、Tool 组和 Product 状态不参与；后续 Step 只计量和拒绝，不自动改写活动组。
- token 计量未启用时，当前显式字节触发模式继续合法；启用时，其触发由完整请求压力替代。
  CompactionPolicy 的摘要字节与保留轮数继续生效，byte trigger 不参与 token 模式判断。
  replacement policy digest 同时绑定原 CompactionPolicy 和 TokenBudgetPolicy digest。
  每条折叠仍 CAS、取消收敛、写后对账；不增加第二压缩 owner、循环重试或副作用重跑。
- 正式 Context/Composition 落盘后，在同一 Session 追加 `request/token-measurement`：绑定 Turn、
  Step、source_seq、composed request fingerprint、Provider/model、算法/编码/库版本、策略、
  分项与总计、触发线、拒绝线。写入采用精确 head 与 owned append；取消等待写入收敛。
  超限记录存在但不进入模型 admission，不写 request/snapshot 或 model/attempt-start。
  写前/写后异常或 CAS 冲突同样不调 Provider；失败 Turn 保留实际已经执行的 Tool/Effect。
- 该记录只是原请求的派生审计事实，不是可变 messages、第二权限账本或实际计费。共享请求重建
  与新 source checker 核对来源、分区、数值、重复记录及越限后非法 dispatch。原 request/snapshot
  形状与 Session 10 / Context 9 / SQLite 2 不变；它是新增可选观测事件，旧读取器没有此能力。
  需要同一编码与库版本才能重算已保存估算；不匹配明确拒绝，不能拿新的算法冒充历史计量。

## 显示、配置与实际用量

F2 新增中文 Token 预算页，提供上述输入；既有启动 profile format 1 增加可选字段，CLI 恢复命令
保留它们并按原 shell quoting 规则编码。配置未齐或互相冲突时启动前拒绝。自动压缩关闭时只有
计量和超限拒绝，不偷偷开启。`tokens` 为可选依赖；缺失明确说明，首次载入编码可能获取公开词表。

状态条显示“最近请求输入估算”，拒绝时显示“超限未发送”；它不是对当前可见 Surface 的实时预测。
详情列出完整分项、配置窗口、预留、余量、触发线，以及该请求最近 Attempt 的实际输入/输出。
`model/attempt-end.usage` 仍是实际用量唯一来源，缺失或非 exact 显示未知，累计用量不能冒充单次窗口。
默认估算不接入现有严格费用 Budget TokenCounter 充当 exact usage，两者的可信等级和用途不同。

## 边界与验证

上下文内部的各类字节安全限额保留，所有选中资料最后一起纳入完整请求 token 检查；E0 不声称
已经按各类 token 配额自动取舍资料。隐藏模板、非匹配模型分词、不同语言和 JSON 封装都可能产生
误差，安全余量不构成永不超窗保证。服务返回 usage 才能做事后偏差校验；D 的模型摘要仍未实现。

验证见 [E0 专题](../validation-request-token-meter.md)。必须有真实普通 Runtime 请求、服务 usage
对照、折叠与重启查证、超限不调用、取消/CAS/失败、重放、中文/代码及错模型绑定反例；不跑全量/L2。

算法选型参考 [tiktoken 官方说明](https://github.com/openai/tiktoken)：本地 BPE 编码可直接计数，
但本项目不会把某个 OpenAI 编码称为任意远端模型的精确计数器。
