# 搜索片段预算追试：到此收口

日期：2026-09-09–10。用户授权再尝试一次，无法验证改善就停止。**本轮 8 对真实问答为 4/8 → 4/8，没有端到端净收益；不采用候选，不追加第二方案、重复或新材料验证。** 生产源码和原发布结论不变。

## 做了什么

从上一轮失败轨迹定位到 `session/reference_search.py::search_page` 的具体行为：预算不足时先缩小片段，缩到宽度 0 后才减少命中条数；减少条数后没有恢复片段宽度。旧轨迹因此只给出两个“呼号”，同时显示扫描 2/13、有下一页。

隔离候选仅在减少命中条数后重新分配片段宽度。它不改原文、匹配、排序、来源、准入、游标绑定或预算，也不指挥模型必须搜索。完整源码与差异见[冻结记录](../validation-data/active-retrieval/reliability-02/frozen.json)、[candidate.diff](../validation-data/active-retrieval/reliability-02/candidate.diff)。示例词和对象 ID 仅出现在固定测试材料中，候选通用代码没有领域特判，防硬编码扫描通过。

## 哪些验证成立

49 项定向检查通过，包括 Memory/History/Skill 搜索及两个实际 Runtime 诊断用例。相同预算下，同样两个命中可恢复周围原文；实际使用一次合法翻页，再用测试材料提供的精确定位词，经原 Reader 读到目标事实。恢复 B0 后，同一公开路径在“片段应有上下文”的断言上失败，见[正向结果](../validation-data/active-retrieval/reliability-02/offline-checks-final.xml)、[反向结果](../validation-data/active-retrieval/reliability-02/reverse-check.xml)。

这里的精确定位词是**测试脚本掌握的线索**，不是模型自行发现，不能记作自主检索成功。另一次连续七页的离线诊断虽然已在搜索内容看到目标，但随后读取/校验耗时较长，被中止；该诊断未完成、不计通过，也没有证明深分页性能或取消收敛。记录见[diagnosis-notes.json](../validation-data/active-retrieval/reliability-02/diagnosis-notes.json)。

## 真实模型结果与限制

复用现有真实运行器，两臂相同模型、预算和隔离直连方式，各跑 8 个冻结场景。没有给真实模型塞入诊断定位词，也没有强制工具调用。原 Session/Effect、成功派发证据、全部回答与人工判定见[review-packets.json](../validation-data/active-retrieval/reliability-02/review-packets.json)、[manual-review.json](../validation-data/active-retrieval/reliability-02/manual-review.json)。

| 指标 | B0 | 候选 |
|---|---:|---:|
| 联合通过 | 4/8 | 4/8 |
| 模型请求/独立重放 | 23 | 21 |
| 工具调用 | 15 | 13 |
| 有命中的搜索页 | 0 | 0 |
| Provider 报告 tokens | 129,308 | 118,755 |

有答案的呼号、被背景遮住的凭证仍未找回；负例仍把“请查交接批准记录”当作凭证；无绑定场景仍把 `source-unavailable` 理解成没有批准事实。直接 Memory/Skill 读取和两道无需检索题通过，日常题没有工具调用。

**本轮关键题没有触发有命中的 Memory 搜索，改动路径并未被真实模型实际使用。** 因而只能说没有验证出自然问答下的收益，不能说已经证明片段改法本身无效，更不能把成本差异归因于该改动。也不据此断言模型已经达到不可改善的上限。

16 条目标旅程无执行错误、无最终连接失败；44 个请求从独立 SQLite 副本精确重放，事件导出相等，不变量与 Attempt 收敛检查通过。共 248,063 个 Provider 报告 tokens，无 unknown/estimated 用量；不把这些数写成字节估计。

## 最终决定

遵守本次停止条件，保留当前生产实现，候选只存档。未跑全量 pytest、L2–L4、Wheel/安装门禁，未提交或发布。编译、修改范围 Ruff、collect-only 和 diff 检查通过。两份项目上下文第 1、7.11 节同步；原 RE-0–RE-5 记录、grid-06 的 51/72 与 NO-GO 不变。
