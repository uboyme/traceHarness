# ADR-0048：Skill 导航元数据与真实 Provider 披露验证

- 状态：Accepted
- 日期：2026-09-08
- 范围：v0.9 F5 内，原 Skill contribution → Context → Tool → 下一 Step 主线
- 前置：[ADR-0043](0043-step-scoped-context-input-and-retrieval.md)、[ADR-0047](0047-literal-query-coverage-admission.md)

## 问题

旧 Skill 顶层已有标题和摘要，但 section/resource/chunk 只有 ID、字节范围和内容摘要值。
`request_skill_reference` 回执也只列 ID；因此可以按已知 ID 取正文，却没有充分提供模型自主选章的依据。
预设工具调用的确定性测试绕过了这个选择过程，不能证明真实模型能够从目录完成任务。

本轮真实调用另外复现两处实现问题：ModelRequest reader 把合法整数温度转为浮点，改变冻结 JSON
指纹并使披露绑定校验失败；NFKC 后的中文冒号被当作代码连接符，整句变成必须精确命中的字面量。
此外，真实模型会把 Skill 目录当成工作区文件，或者在读下一章前遗漏当前仅存在一个 Step 的正文。

## 决定

1. `SkillSection`、`SkillResource`、`SkillChunk` 都要求贡献方显式提供非空 `title`、`summary`。
   构造参数为 required keyword-only；序列化 reader 拒绝缺字段。不得从 ID、路径或正文猜标题，
   不调用模型生成隐藏摘要。全部元数据进入原 catalog digest 和 Composition revision；单项 summary
   沿用 `max_summary_bytes`，目录总体仍受 `max_catalog_bytes` 限制。
2. `SkillDescriptor.navigation()` 只从冻结元数据派生导航卡：section/chunk 含 ID、title、summary、
   content_bytes；resource 另含 relative_path 和 chunk 卡。`directory()` 加上 Skill 顶层身份、标题、
   摘要与标签。Context directory 与 Tool available 复用这两个投影，不扫描资源或复制正文。
3. Context renderer 为 Skill item 给出 `skill_notice`；宿主固定 system 说明只解释引用机制。
   live PromptAssembler 与 Generation 的 frozen assembler 共用 `assemble_prompt_sections()`，
   避免提示只接到未被生产请求使用的装配分支。Skill 内容本身仍不进入 system。
4. 工具描述和回执说明实际位置：下一请求的第一条 user message 是当前 Step 的参考；读取后才能回答。
   多章对比可以在一个模型响应里发出多条原工具请求，使正文同时进入下一 Step；顺序阅读时模型可以
   先在自身回答中记录相关事实。这些模型文字不变成宿主权威，宿主不复制正文到 Surface。
5. 正文继续只授予同 Turn 紧邻下一 Step。原 selection、Lease、预算、取消、失败、重放、拒绝和权限
   规则不变；不增加跨 Step 持有、pending 缓存、全文搜索、批量新工具或第二事实源。
6. ModelRequest reader 保留合法 JSON 数字的原表示，拒绝 bool、字符串及非有限浮点温度。
   无引号的普通词尾句号/冒号，以及仅由冒号连接的汉字语句，按散文处理；显式引号、复合代码标识
   与路径仍保持整体。没有具体领域词表、样本名单或新排名阈值。
7. 当前协议唯一切到 Session 6、Context 5、renderer `context-json-v5`、tokenizer `traceh-lexical-v3`。
   policy `f4-context-policy-v2`、来源 receipt 2、ranker `eligible-bm25-v2`、SQLite 2、M3 2 不变。
   旧 Session 1–5 明确拒绝，旧描述符也拒绝；不迁移、不双读、不删除旧数据。

## 验证与限制

程序回归检查生产请求中的导航、元数据绑定、旧协议拒绝、预算整块排除、取消收敛与历史重建；
关键反例在恢复旧逻辑的独立源码副本中验证，不能靠解析异常或未进入目标路径证明修复。

新增的是 [显式真实集成测试](../../tests/live_skill_navigation/README.md)，不是第二个产品 Benchmark
引擎或 `traceh eval` 入口。它用原 Provider/Runtime/Plugin/Store/Tool，从自然语言任务自主产生调用，
按模型分别报告任务完成、严格选章、多余读取、工具/Provider 失败和重放证据，并保留导航移除对照。
不构建 Wheel，不自动执行真实调用，不运行全量或 L2；原 Product 检索查询、判定和阈值不修改；新增目录字段改变 catalog digest，
仅重冻结该协议绑定及其 corpus 文件摘要，保留原摘要和拒绝证据。

语义召回、隐藏 Skill 搜索、模型可靠性保证仍不在本次实现范围。模型可能忽略工具说明或多读内容，
不同模型必须单独报告；完整结果见 [验证记录](../validation-v0.9-skill-navigation.md)。

## 后果

第三方 Skill 贡献需要显式补齐导航字段并使用新数据目录。目录和提示消耗额外可见字节，按原统一预算
精确计量，超限整块拒绝。持久化权威、资源 owner 和主请求生命周期保持单一；Release Stop C 与发布
候选门禁仍单独进行，本 ADR 不表示这些门禁已经通过。
