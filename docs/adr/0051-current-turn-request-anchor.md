# ADR-0051：后置参考包绑定本轮原始请求

状态：接受。日期：2026-09-08。范围：修复完成检索后换话题仍重复旧答案；保持 ADR-0049 的参考后置及同 Turn 有界披露。

## 证据与决定

用户会话的新问题已进入独立请求，HTTP 适配器没有复用旧请求或答案。真实对照中，“你好”和“你是谁”
两条冻结请求原样重发均重复旧答案；保留参考包并在末尾明确标出当前问题，两条都正常回答。移除参考包
并同步修改定位说明的对照也正常，但删除参考包会损失已有披露能力，因此不采用。

1. Context receipt 增加必填 `active_request={source_ref, content}`，由当前 Turn 首条真实 `user/message`
   派生。原文不规范化、不截断，不从 query、摘要、工具回执或后续 verifier feedback 猜测。后续 Step
   继续指向同一原始输入；新 Turn 重新绑定。已有 source qualifier 和 Session CAS owner 负责读取、验证和写入。
2. 唯一 renderer 在原参考内容之后追加逐字 JSON 引用的本轮问题及通用定位说明；提醒使用本 Turn 的
   Tool/verifier 结果完成它。宿主标题由 renderer 生成，用户原文仍是用户输入，不升级权限。System
   说明对应定位规则。Surface 不保存参考包或这份派生回显，不新增消息事实源、分类器或缓存。
3. `validate_context_input_sources()` 从原 `observed_session_seq` 边界独立重建并比较整个绑定；即便
   攻击者同时重算 Context digest，替换原文或借其他 Turn 的 EventRef 仍拒绝。没有真实输入就明确失败。
4. 引用预算仍由 `ContextInputPolicy.total_bytes` 限制资料 wrapper、导航及正文，准入顺序和整块淘汰不变。
   用户问题回显属于必需的请求输入，按实际来源长度单独记账，不挤占也不扩大引用授权：
   `reference_limit=policy.total_bytes`，`reference_bytes=引用呈现字节数`，
   `active_request_bytes=原文回显与定位说明的实际 UTF-8 字节数`；
   `rendered_bytes=reference_bytes+active_request_bytes`，`total_limit=reference_limit+active_request_bytes`。
   `remaining_bytes` 只表示剩余引用预算。最终模型请求包含全部字节，仍走原 token 计费/预算与派发许可证。
5. 构建、冻结、请求校验和重放继续共用原 Context renderer 与 RequestBuilder，不在 Provider 发送前
   追加未冻结提示词。Inspector 看到同一来源绑定与预算。
6. 唯一协议切换到 Session 10、Context format 9、renderer `context-json-v9`。policy v5、SQLite 2、
   M3 2、披露收据 2、检索策略与 tokenizer 不变。旧 Session 1–9 明确拒绝；不迁移、不删除、不改写用户旧账。

## 验证和边界

定向验证覆盖空参考、原文/来源伪造、字节记账、工具续步、失败后新任务、历史分页、Memory/Skill、
恢复/重放、不变量、Inspector 和相邻 Product 请求。关键绑定检查必须反向验证。

真实例子使用隔离 SQLite、生产 Runtime/Provider 与显式历史读取政策，不脚本化模型回复。
强制每页两条消息时发现模型提前停止翻页；修改前呈现的对照也失败，额外提示未解决，因此撤回该无效
提示，单列为既有模型使用分页的边界，不宣称本 ADR 修复了它。相关失败、重试及最终结果见
[验证记录](../validation-current-turn-anchor.md)。本次不运行全量或 L2，不改变旧 ADR 的历史决定。
