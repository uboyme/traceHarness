# ADR-0053：分层压缩合同与可读回的大工具输出

状态：A/B 接受；C/D/E 仅为后续边界，尚未实现。日期：2026-09-08。

## 问题与范围

M3 在新 Turn 前替换闭合对话前缀，默认摘要器是无模型的有界摘录。ToolRuntime 原先在
写 Effect Outcome 前截断正文；单靠保留 Session 事件无法恢复已经截掉的文本。现有 History
保护整个 Turn 的分页边界，过大的 Turn 也可能无法披露。因此不能先折叠输出，再假设原文总能找回。

本轮完成 A 的分层/预算合同，以及 B 的正常工具执行→持久输出→有限读回→恢复闭环。
不实现旧结果微压缩、模型摘要、轮内压缩、Provider 超限重试或新的 TUI 设置。

## 决定

1. **沿用 Effect owner。** 大输出的原始 content、结构化 data 与 evidence 保存在同一
   `effect/outcome.retained_output` 中；同一事件同时记录 `output_ref` 和实际呈现预览。
   使用原 SessionService→EventStore 的一次 SQLite 事务，不引入外部文件目录、CAS、垃圾回收器
   或第二次提交。存储位置是当前 data directory 的事件数据库，不是源码目录。
2. **完整保存与有限展示分开。** content 或 canonical data 超过既有 `max_tool_output_chars`
   时，Outcome/Result 的呈现内容改成预览、来源和读取动作；Result.data 为空，原始结构化数据
   从 retained payload 读取，防止 stdout 等通过 data 再复制回 Session。小结果仍原样 inline。
   该阈值是字符限额；预览外的固定说明、引用及 JSON 转义另外计入实际展示成本，不冒充 token。
3. **引用绑定原执行。** format-1 引用包含 effect_id、payload digest、正文字符/UTF-8 字节数、
   canonical data 字符数。纯 resolver 检查同 Session 的 Tool Call、Tool Result、Effect Intent、
   Outcome 的数量、身份、Step、参数、因果链接、状态和呈现一致性，并重算 digest/长度。
   同一工作区的另一个 Session 不能使用该引用。引用本身不是越过 Session 的能力令牌。
4. **读取走普通 Tool 主线。** 默认工具增加 PURE_READ `list_tool_outputs` 和 `read_tool_output`。目录从本 Session 原结果派生，
   即使完整 History Turn 超过页限额也能定位引用；通过 through_seq 固定后续 offset 的目录边界。
   读回输入 effect_id、digest，
   可选 part=content/data、offset、count；偏移单位为 Unicode code point。完整 JSON 页受当前
   max_tool_output_chars 约束，返回 next_offset 或 null；预留不够容纳一页时明确失败，不截坏 JSON，
   不制造循环引用。读回旧输出不会执行原工具、读取当前工作区文件或提升为批准 Memory。
   `include_default_tools=False` 的宿主须自行装配读取工具；未注册时预览不宣称有可调用 read_action。
5. **生命周期继续由原 owner 负责。** 大输出正文和引用一起提交，提交失败不会留下已发表的悬空指针。
   Outcome 后 Result 前崩溃/取消由原恢复与取消收敛路径补齐同一个 Result；不重复执行工具。
   持久化放在工具异常处理之外，写入错误不得伪装为工具执行失败或追加互相矛盾的 Outcome。
6. **导航必须接线。** 原通用 reference prompt 区分聊天 History 与保留的 Tool output；
   工具正文页直接在 Tool Result 中，不冒充最后 Context 包里的披露正文，也不表示批准 Memory。
   真实运行已发现只注册工具而不说明两类来源会导致模型在过大 History 页上反复失败。
7. **重放与审查复用。** Session 冻结实际呈现的预览/页正文，原 RequestBuilder/Surface 重放不读取
   latest 输出或重新调用工具。CoreInvariantChecker 在提供 Effect 流时复用同一来源 resolver；
   仅提供 Session 流的检查不宣称已核查 Effect 来源。

## 分层与预算冻结

后续顺序为保存大结果→折叠旧结果→摘要旧对话→需要时重建上下文。
当前用户原话、未完成 Tool group、近期必要结果、系统/工具定义及 Product 权威状态受保护。
整段替换必须以完整闭合 Turn 为边界；旧结果正文折叠必须保留整个调用/结果协议组。

完整请求预算应覆盖 system、tool schema、Product、Surface 对话、Context reference、当前任务回显、
Provider 封装及输出预留。精确字节与估算/实际 token 分开；无可信 tokenizer/模型窗口时不得显示
虚假的占用百分比。80% 触发、60%–65% 目标仅是待真实评估的候选比例，不是生产默认值。
当前仍使用 M3 的显式字节阈值，不声称 B 已限制整个请求或一整批工具的总大小。

模型摘要须另行扩展现有可计费、可取消、可审计的 Provider 请求协议，绑定冻结来源与摘要结果；
不得在 SessionSummarizer 里裸调 API。摘要是带来源的不可信历史，不能写批准 Memory、代替
Product 状态或把执行未知改写成成功。新的请求必须继续保留 ADR-0051 的当前任务定位。

## 协议与边界

保留输出是现有 Effect/Tool Result 内新增的明确 variant，format 在引用中为 1；不是新的 Surface
替换协议。Session 10、Context 9、SQLite 2、M3 format 2 不变。已保存的旧截断文字不被重写，
不存在的原文不凭空补齐；没有旧协议 parser、自动迁移或删除。

B 保留的是 ToolRuntime 实际收到的文本/数据。底层工具自己限制行数、Shell UTF-8 解码替换、
工具还未返回时的取消/Runtime timeout 不会因此获得不存在的原始字节。普通失败和工具自报 timeout
交出的长文本同样保留；traceback 仍沿用原诊断尾部上限。非零退出码与宿主调用成功是不同字段，
读取时保持原 exit_code。

大输出仍占数据库磁盘，读取器当前读取本 Session Effect 流，再做有界披露；没有流式存储、磁盘配额、
保留期限或垃圾回收。B 不消除无限输出风险，也不修复历史目录自然语言召回的所有语义局限。

## 验证

定向测试：真实 Shell + SQLite、非零 exit、Unicode/JSON 页界、data 读回、重启和 M3 后来源读取、
跨 Session/错 digest/错执行身份拒绝、提交后错误和重复取消收敛、恢复幂等及请求重放。
另运行明确的真实 Provider 脚本 `tests/live_tool_outputs/run.py`，使用随机合成观测、自然提问、
压缩后重启及新话题，检查实际读取调用和唯一执行计数。最终结果记录在专题验证文档。
