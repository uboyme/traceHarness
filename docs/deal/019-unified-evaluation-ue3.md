# 019：UE-3 隔离变体与候选比较

日期：2026-09-10。UE-3 实现、定向门禁和真实 A/A 验证完成；未提交或发布。真实语义评分仍待人工审阅。

## 修改与原因

沿用 EvaluationRunner 的试次生命周期，增加两臂冻结与独立进程装配，避免把候选文本热补丁装进活跃 Runtime。
候选只可修改宿主列出的说明字符串，AST 校验保证 grader、预算、权限、执行代码和事件协议不变。
离线 comparison 与 review 共用原运行证据校验；原 worker 回执绑定原报告摘要，人工评分按不可变 judgment 重算。
报告分别描述质量、成本、执行失败与未证明项；比较不赋予采用权限。

未保留被替代的第二评分路径，没有重置用户数据或修改用户配置。真实模型和归档检查只通过原加载器在进程内使用现有凭据，不显示或保存原值。
UE-0～UE-2 的单变体入口仍是同一主线的有效模式，不属于旧协议兼容；旧实验记录保持历史身份。

## 验证记录

初轮定向检查的两个断言已定位：一个误用 SQLite 列名，另一个忽略了
“答错且 token 更多，但工具调用更少”应同时展示两种成本变化。前者修正测试查询，后者明确 quality_status
与总 status 的区别，不为了得到预期标签改变真实测量。

Product 双臂原任务执行、Git 验证与推广通过，但 Windows 临时目录关闭检查发现离线统计连接残留。
根因是 sqlite3.Connection 的 with 只管理事务，不负责 close；comparison 读取层改用 contextlib.closing。
新测试通过公开 compare 执行捕获实际连接，确认完成后每个连接都已关闭；移除 closing 后在真实运行完成的前提下失败。
另一个反向验证去掉 worker 原报告摘要守卫，修改原报告分数不再被判为 not_comparable，新测试因此失败；恢复保护后两项通过。
Product 测试还修正了推广流的断言，使用原 PROMOTION_LEDGER_STREAM，不自造流名前缀。

| 定向检查 | 结果 |
|---|---|
| comparison、comparison_evidence、episode_review_contract、evaluation_contracts、evaluation_architecture、product_architecture | 93 passed |
| cli_env、cli_read_only_commands、openai_provider、promotion_architecture | 65 passed |
| Product 双臂、comparison_evidence、强制 worker 退出 | 14 passed；与首组有重叠，不累加 |
| evaluation_lifecycle、retrieval_episode_evaluator、episode_dispatch_binding | 23 passed |
| 真实本地 HTTP：故意退步、断连、重复取消 | 已在先前定向组通过；强制退出另在 14 项收口中通过 |
| 两项保护反向验证 | 各移除保护后按预期失败；恢复后 2 passed |
| compileall src/tests；修改范围 Ruff | 通过；Ruff 44 个实现/测试文件 |
| pytest --collect-only | 3848 tests collected；只收集，未执行全量 |
| 文档章节/相对链接/代码块、示例硬编码扫描、git diff --check | 通过 |

取消测试在实际 HTTP 请求开始后才发出取消，用 Gate/Event 控制收敛，不用“根本未执行”的空验证。
故意退步候选走两个实际 worker 和原 Provider HTTP 客户端，保留质量退步与成本有好有坏两种信息。
错基线、越界/重复编辑、篡改原证据/报告、错误评分引用、未知用量、不匹配材料和完整分母都有公开反例。

## 真实 A/A

使用显式现有 qwen-plus 配置、原 v0.10 Docker 沙箱镜像和直连网络；没有拉镜像、构建、改配置或隐藏重跑。
两臂都是同一冻结源码，各运行八条选择题，过程不改源码、材料、准备规则或模型。
Run `366e729d-c6c4-4f08-a50a-8ecb4cc8b211`：16 条全部完成关闭，准备生成文字摘要也一致。
两臂分别 241,784/243,031 exact token、21/21 次工具调用，共 102 次模型请求，无失败 Attempt。
8/8 正例有回答前实际派发证据；16 条仍 pending_review，因此比较为 complete=true / inconclusive，不能宣布语义全通过。
总成本 484,815 Token，A/A 多 1,247（约 0.52%）；只描述这一轮波动，不做统计显著性或提升率结论。

关闭后独立重开 20 个 Session，102 份请求重放和 CoreInvariantChecker 全部通过；不调用 Provider。
再次离线比较与原结果一致，JSON/Markdown 相同。真实原请求、源码、材料、账本、进程回执、逐条原回答、
空人工评分模板和摘要清单见 [UE-3 证据](../validation-data/unified-evaluation/ue3/README.md)。
归档前递归检查实际密钥字节及非空 .env，无实际凭据；原始实验归档还经过搬移后的离线比较验证。

预审仍看到 Skill 负例从手册主题/摘要推断“不存在”，另有否定范围需人工核对；已留下明确的 agent advisory，
没有把 Codex 预审冒充人类评分，也没有以修复质量为由越过 UE-3 去调整提示或重跑 72 条。

## 文档和边界

字段与生命周期见 [UE-3 合同](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE3_CONTRACT.md)。
正式版和通俗版按相同编号同步 1、3、12.6、13、14、15、17，更新模块职责、生命周期、状态、配置、验证和 Mermaid。
README、CHANGELOG、统一设计/合同/ADR 实施状态及题库双臂示例同步，ADR 原设计决定未改写。
反硬编码扫描未发现本次故事、模型、材料种子、case ID 或本机路径进入通用实现；候选和错误条件用独立夹具验证。
依赖清单仅记录环境身份，远端模型 revision 仍未知；可信文本候选进程隔离不代表恶意 Python 沙箱。
强制杀死直接子进程不保证原业务资源已收敛，继续保留 unproven，不启动另一臂；没有冷恢复或自动采用。
不进入 UE-4、AO、MCP 或动态 Workflow；不跑全量、L2–L4、Wheel、发布或自动采用。
