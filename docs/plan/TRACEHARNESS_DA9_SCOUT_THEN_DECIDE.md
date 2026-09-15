# DA-9：有界只读侦察后再决定协作

> 状态：首组真实实验结束；候选已撤回，未启动确认组。前序依据：[DA-8 记录](../deal/034-exclusive-adaptive-decomposition.md)。

## 问题与唯一实验变量

DA-8 可以使主 Agent 完成独占决定，却要求它在没有工作区证据时一次选定 local/separable。DA-9 只验证“先取得少量实际读取证据，再决定”是否改善该行为。沿用三类既有材料、相同需求、模型、预算、断网 Docker Verifier；不加入题目关键词分类，不改语义评分，不把文件结构检查称为语义正确。

## 本轮合同

- 仅显式 adaptive 的主 Agent 启用；single、普通 Chat 和只读助手保持原路径。
- 每个新 Turn 的前两个 Step 只公开调用方原已授权的 list_files/read_file/search_text，每 Step 最多执行两个侦察调用；原 Token、工具调用、时间和 Step 预算照常收费，不增加额度。单页沿用现有 ReadFile 页上限。
- 第三个 Step 进入独占 decide_task_decomposition；决定前必须至少有本 Turn 侦察阶段成功的 read_file 回执，否则明确失败。目录/搜索命中本身不算已读文件。决定回执自动附带精确读取身份及摘要，模型不能自填“已经读过”。
- local 不创建助手；separable 必须提交不同的主子目标、只读独立声明、交付要求和 briefing，复用原 delegate_investigation。程序只能校验结构与权限，不能证明自然语言子任务实际上独立。
- 决策最多三次尝试，最多两次同文修复提示；漏答、非法或重复决定不能变成完成。原预算、最大 Step、拒绝循环和失败停止条件优先。
- 已派出的初始助手必须按原 Session 中精确 agent_id/message_id collect 到非 pending 回执或显式 stop 后，主方才能正常完成。失败回执不等于成功交付；验收另查实际报告进入主方请求。
- 阶段状态只从同一 Session/Turn 的事件读取。Composition 和 Request 保存实际工具面；隐藏工具在执行前拒绝。所有副作用、取消、工作区和预算继续由原 owner 管理。

## 架构与停止规则

Product 拥有侦察/决定/执行语义。Runtime 如需接缝，只提供通用 Step 工具面收窄及预算结算后的 Continuation 修正；不认识 Product，不新建状态机事实表、Projector、Workflow 节点或第二消息源。生产接线必须匹配 Product Registry 冻结的工具、提示和策略身份。

先完成确定性正常、反例、失败/取消、恢复读取、批量越界及预算先停止验证；关键保护进行反向测试，再跑相关与相邻 owner 测试和真实 Docker 完成/取消路径。禁止全量 pytest、L2–L4、Wheel、提交、推送及发布。

真实阶段冻结一组三题：可拆、紧耦合、简单，各一次；qwen-plus 直连、单请求一次尝试，调用上限沿用 DA-8 的 24/12/8，合计最多 44。保留失败，不因错误答案重跑。仅当首组全部通过，才允许同合同另跑一组确认重复性；网络失败独立记录并停止，不自动重试。采用条件为全部任务硬通过、侦察证据实际进入决定请求、预期 separable/local/local、可拆题有真实子模型调用与原报告交接，预算全部收敛且 Workspace 无泄漏。

任何工程边界缺陷先修正再验证；任何分类或行为门槛不通过，保存代码快照和证据、撤回生产候选，并报告假设未获支持。不得靠额外侦察回合、默认加预算、改题、改 verifier 或幸运重跑追分。

## 实际结果

候选确定性检查与真实 Docker Product 收回/取消主线通过。首组三题共 18 次 qwen-plus 调用、65117 exact tokens；三次侦察结果均进入决定请求，但可拆和紧耦合题在工具集合切换后仍继续请求读取，达到三次决定尝试上限而失败。简单题也先被拒绝两次，第三次才选 local 并完成。联合行为仅 1/3，无助手、无 Provider 失败、预算全部收敛、Workspace live=0。

9 个会话、24 份原请求在临时库重放与不变量检查通过，227 个原文件不变。由于首组未达门槛，没有第二组；候选已完整归档并撤回，生产维持原 adaptive、默认 single。两步侦察不能保证获得足够的拆分证据，阶段切换也未被模型稳定遵循，故不能据此宣称“侦察方向无效”或“模型分类能力已经被测清”。详见[记录 035](../deal/035-scout-before-decomposition.md)与[验证数据](../validation-data/dynamic-collaboration/scout-decomposition/README.md)。
