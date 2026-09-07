# TraceHarness v0.9 Release Stop A 独立审查与 P2 修复

日期：2026-09-07。对象：F1 Skill 贡献机制与 F2 持久选择、检索、模型渐进披露。

## 1. 结论与停止点

**首审 P0=0，P1=0，P2=2；Release Stop A 的独立审查门槛通过。两项 P2 随后已修复，当前已登记 Finding 全部关闭。**

按 [AGENTS.md §8](../../AGENTS.md) 与 [阶段计划 §9.3](TRACEHARNESS_V0.9_STAGE_PLAN.md)，
P2 默认不阻断本停止点。首审未改生产代码或仓库测试；用户随后授权修复两项 P2，修复与验证另见第 7 节。
F3–F5 未开工，版本仍为 0.8.0。此结论不代表检索质量评测、全量或发布门禁通过。

三个独立分区分别审查 Plugin/Generation、selection/Store、retrieval/Request；
主审交叉核对公开调用与合同，并独立运行两项 Finding 的反例。没有以实现者此前的通过报告代替审查。

## 2. 首审冻结范围

- 基础 HEAD：`22799a3de15890fe92f8a478edea366543428e6e`。
- 冻结当前 F1＋F2 工作树 48 文件：23 个生产 Python、17 个测试/夹具 Python、8 份文档，
  包含新建且未跟踪的 F1/F2 文件；不把 HEAD 或只看 tracked diff 当作当前实现。
- 审查完成、登记文档前，48 文件逐字节 SHA-256 与开始时一致；登记后生产及测试 40 文件仍不变。
- 公开入口：插件 setup/register_skill 与 Generation/Lease；
  `runtime.skill_context.select/rebuild_index`；真实 Runtime 的 `run_existing`；
  普通 `request_skill_reference` Tool；Session reader、SQLite query/rebuild/close/backup。
- 威胁边界仍是 trusted、进程内 Plugin；未扩大为任意敌意 Python 对象或 OS sandbox。
  不以 F3 Memory、F4 跨来源配置、F5 UI/质量评测、semantic/reranker 等未来能力提出实现 Finding。
- 用户已有的其他未跟踪文件与目录未纳入、未修改。没有读取真实 .env 或秘密。

两项 Finding 对应审查时源码 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `session/context_input.py` | `80bf33d14e3994d07728c0d3bb537f3edc8941c4bc52a62036033e55fe50ed82` |
| `session/skill_retrieval.py` | `fd30f37451ede969fc7a3b683c83ac2ea9147c61aa9a0527d14cec2415191734` |

## 3. 首审 Finding（两项现均已修复）

以下触发和输出对应第 2 节记录的修复前源码，保留原始证据；当前实现与新增回归见第 7 节。

### A-P2-01：同一 Skill 的目录与摘要双请求导致下一 Step 失败

位置：[context_input.py](../../src/traceh/session/context_input.py) 审查时 518–532 行，
`parse_context_input` 的块重复身份检查。

**公开路径与复现步骤：**

1. 用现有 [retrieval_fixtures.py](../../tests/retrieval_fixtures.py) 的
   `sample_skills/build_case/select` 创建真实 Runtime、SQLite 和已启用 Skill；宿主选择第一项并重建索引。
2. 用户输入第一项的 Skill id，让第一 Step 按默认测试配置实际披露其摘要。
3. Scripted Provider 第一次响应返回两个不同 call id 的普通 `request_skill_reference` ToolCall。
   两者的 skill_id/version/catalog_digest 均来自本次装配，只分别设置 requested_tier 为
   `directory`、`summary`，section_id/resource_id/chunk_id 均为 null。
   第二次预设模型响应为普通完成；测试预算足够容纳两块。
4. 两次 Tool 都成功，下一 Step 在 Context 解析时抛出
   `ContextInputError: context-source-duplicate`，第二次 Provider 调用没有发生。

主审实际输出：

```json
{"failure":"ContextInputError: context-source-duplicate","provider_calls":1,"tool_results":[{"status":"succeeded","tier":"directory"},{"status":"succeeded","tier":"summary"}],"context_count":1,"last_event":"turn/end"}
```

**根因与现行合同：**去重仅使用 `(id, provenance)`，directory 与 summary 的 provenance 相同，
遗漏 tier；现有 `skill_retrieval.block_identity()` 及
[设计合同 §14.2](TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md) 的内容身份明确包含 tier。
普通 Tool 已接受两项不同层级的合法请求，Context 却将其合并为重复来源。

**影响与等级：**正常多 Tool 请求中断当前 Turn。失败前没有第二次 Provider 派发，Turn 已闭合，
未证明权限越界、持久事实损坏或生命周期逃逸，按现有证据列 P2。
现有四级披露测试逐级独立执行，没有覆盖同 Skill 两层级在同一目标 Step 共同出现。

**建议修复 owner：**在已有 Context 块身份规则中统一 Skill 的内容身份，复用包含 tier 的既有身份定义；
补充混合层级公开请求回归及真正重复项反例，不增加第二套状态或披露权限。

### A-P2-02：含空格的合法资源路径无法精确命中

位置：[skill_retrieval.py](../../src/traceh/session/skill_retrieval.py) 审查时 236–249 行，
`rank()` 的 exact lane 匹配。

**公开路径与复现步骤：**

1. 继续用现有 `sample_skills/build_case/select` 与
   [skill_fixtures.py](../../tests/skill_fixtures.py) 的 `with_resource/policy`。
   给第一项 Skill 声明真实资源文件及宿主显式 `SkillResourceRoot`。
2. 显式配置 `match_fields=("path",)`，宿主选择该 Skill 并重建索引。
3. 两个隔离的同装配示例分别使用资源相对路径 `manual.txt` 和 `manual pages.txt`；
   当前用户输入分别等于对应完整路径。资源正文与其他元数据不包含该查询文本。
   这些名称仅为复现夹具，不是系统默认值。
4. 无空格路径 exact 命中；含空格路径两个 lane 都 available，却没有候选，记录 `no-hit`。

独立审查者与主审各自运行真实 Runtime→select→rebuild→run 路径，结果一致：

| 示例查询 | exact | FTS | Context |
|---|---|---|---|
| `manual.txt` | 一项命中 | 空 | 注入已选 Skill 摘要 |
| `manual pages.txt` | 空 | 空 | blocks=[]，Skill 排除原因为 no-hit |

**根因与现行合同：**query 先由正则表达式提取无空格 symbol，再将完整 relative_path
与 symbol 集合进行相等比较，含空格路径因此无法命中。FTS 语料没有资源路径，不能补足。
路径已经通过资源贡献和读取边界，并不属于无效输入；
[正式上下文 §7.5](../note/project-context.md) 与当前配置公开提供 exact path 匹配。

**影响与等级：**已选、可用资源的合法精确查询漏检，没有权限或事实源破坏，列 P2。
现有检索用例未覆盖含空格的合法路径。

**建议修复 owner：**在现有 exact matcher 保留完整标识的匹配语义；
补充含空格路径与近似但不相等路径的公开回归，不靠增加 FTS 正文或放宽 eligibility 规避。

## 4. 首审已验证事实

| 分区 | 核对的当前合同 |
|---|---|
| Plugin/Generation | setup 注册身份与预算、host root、UTF-8 bytes/digest、ActivationSet 归属、旧 Lease 冻结字节、新代读取、重复取消/释放与 Drain cleanup |
| selection/Store | 同一 Runtime Lease 与 SessionService owner；exact operation 重入、CAS 和失败三态对账；source-head 原子重建、回滚、精确 schema/shadow 拒绝、close/backup 的原 owner |
| retrieval/Request | eligibility 先于 corpus/BM25；请求来源、实际 ToolCall 与已披露身份；紧邻 Step 消费；失败/预算/取消不顺延；历史重建不查当前索引/资源；同 Step retry 不重检索 |
| 主审交叉核对 | Context/Request 协议、Product/SQLite 架构保护；两项 P2 的真实运行反例与现行身份/检索合同 |

未发现其他符合 AGENTS §8 证据准入的 Finding。没有把“没有发现”扩大为所有平台或输入上的正确性证明。

## 5. 首审实际验证与未运行门禁

所有 pytest 批次均从独立空临时 cwd 执行，传绝对文件路径、独立 cache/basetemp 与 `-o addopts=`，
使用 `python -X utf8 -m pytest -q --tb=short`。没有扫描整个 tests 目录运行测试。
环境为 Windows、Python 3.13；各分区可以并行，但没有用 pytest-xdist。

| 批次 | 明确文件集合（均在 tests/） | 实际结果 |
|---|---|---|
| Plugin/Generation | test_skill_contributions.py、test_skill_resources.py、test_plugin_activation_sets.py、test_plugin_composition_coordinator.py、test_composition_generations.py、test_plugin_manager.py、test_plugin_runtime.py、test_plugin_sdk.py | 205 passed / 1 skipped，5.02s |
| selection/Store | test_skill_selection.py、test_context_index.py、test_sqlite_event_store.py、test_sqlite_event_store_architecture.py | 40 passed / 2 skipped，3.60s |
| retrieval/Request | test_skill_retrieval.py、test_skill_context_failures.py、test_context_request_protocol.py、test_model_retry.py | 79 passed，16.37s；同集合 collect-only 79 项 |
| 主审相邻回归 | test_context_request_protocol.py、test_product_architecture.py、test_product_contract.py、test_sqlite_event_store_architecture.py | 119 passed，3.27s |
| 临时目录独立并发探针 | Runtime dispose 等待 selection Lease、dispose 后拒绝；两套真实 Runtime/SQLite 共享数据库时相同/不同 operation 的 CAS 与精确对账 | 3 passed，0.53s |

各批次有重复文件，**不相加为去重测试总数**。跳过项为 Windows 符号链接权限边界；
原始 `-q` 输出未逐项打印 skip reason，具体原因由对应测试源码定位。
两项 P2 由额外公开路径探针复现，不是绿色 pytest 已覆盖的行为。

主审保存了相邻回归、两项反例的原始输出；独立分区的真实工具输出和确切命令已交叉核对。
40 个改动 Python 文件 Ruff 通过，源码/测试快照未变化。
本轮只修改审查文档，未重跑此前实现阶段的 compileall 或反向保护。
文档 QA 覆盖 9 份文档：481 个相对链接有效、30 个 Mermaid 块闭合、两份上下文 0–20 章节对应，
没有新增秘密形态或编码损坏；原架构图未变化。修改范围空白检查通过。

此前 F2 的 45 文件 `1076 passed / 4 skipped` 是
[阶段计划 §9.4](TRACEHARNESS_V0.9_STAGE_PLAN.md) 的实现证据，不是本轮新执行，
也不能覆盖本轮发现的两项 P2。

**未运行：**全量、L2–L4、Wheel/安装、联网包索引、真实 Provider/API、其他操作系统/SQLite 组合、
检索质量评测。没有本轮网络故障或单独复现的 HEAD 基线失败需要归因。
没有 commit、push、tag 或 release。

## 6. 首审文档同步与后续边界

先更新正式上下文，再据此同步通俗版：第 1 节当前阶段、第 7 节 F2 已知限制、
第 15 节本轮审查证据、第 16 节两项未修复边界、第 20 节当前阶段计划
（正式版 20.33／通俗版 20.27）。

同时纠正第 16 节“Context 新协议、FTS 未实现”的遗留描述，
以及第 20 节仍把 F0-C 窄配置称为当前策略的旧表述；当前协议/配置事实未改变。
同步阶段计划、设计合同实施状态、总路线、ROADMAP 和 CHANGELOG；
ADR 历史决策与架构图不改写，现有图的 owner/事实源流程没有改变。

首审建议随后修复 A-P2-01 与 A-P2-02；用户已授权且修复完成，见第 7 节。
P2 按既定规则不自动阻断 Stop A；首审及此次修复均未提前实现 F3。

## 7. 两项 P2 修复与定向确认

用户在首审后授权修复两项 P2；本节记录修复后的实际代码与验证，前六节的首审数字和原始失败证据不改写。

### 7.1 修复范围与当前行为

仅修改两个生产文件与对应两份测试：

- [context_input.py](../../src/traceh/session/context_input.py) 复用
  `skill_retrieval.block_identity()`，包含层级、版本、来源标识和内容摘要。
  同 Skill 的 directory＋summary 两种请求顺序均能成功冻结到紧邻下一 Step 并实际派发模型；
  同层级重复请求仍由现有 receipt 去重，持久 Context 中真正重复的块仍拒绝。
  History 的原页身份、来源校验与预算没有改动。
- [skill_retrieval.py](../../src/traceh/session/skill_retrieval.py) 对 NFKC+casefold 后的完整字段值
  做字面量匹配，转义正则字符，保留内部空格并沿用标识字符边界。
  已选资源路径可独立输入或出现在引号内；前后多出标识字符、扩展名后缀或父路径时不误命中。
  FTS 仍只索引原有目录元数据，资源正文不进入检索语料。
- [test_skill_retrieval.py](../../tests/test_skill_retrieval.py) 新增 18 项：
  两种混合层级顺序、相同请求合并，以及三种完整路径的独立/引号查询与三类近似反例。
  路径包括空格、括号、加号和中文；这些都是显式夹具，不是生产默认值。
  测试经过真实 Runtime、SQLite、宿主选择、索引和模型请求，并核验历史重建、不变量及下一 Turn 不继承披露。
- [test_skill_context_failures.py](../../tests/test_skill_context_failures.py) 新增一项：
  从真实持久 Context 构造重复块，重算预算和 Context digest，公开 parser 仍准确拒绝 `context-source-duplicate`。

本次修复没有新增事实源、状态机、权限、持久字段、兼容模式或协议版本。
原冻结请求仍按原证据重放，不重新运行当前 exact matcher。没有开始 F3。

### 7.2 反向证据与最终门禁

新增 **19 项通过**后，临时逐字节恢复两处修复前的生产文件，运行两个新增 Runtime 测试组：

- 两个混合层级顺序再次因 `context-source-duplicate` 失败；
- 三种路径的独立和引号查询共六项再次因 exact ranking 为空失败；
- 其余十项保持通过；合计 **8 failed / 10 passed**，没有导入或夹具类型错误充当失败证据。

随后逐字节恢复修复代码，再执行最终明确限定的 12 文件集合：

| 最终验证 | 结果 |
|---|---|
| 同集合 collect-only | 273 collected，0.83s |
| 同集合定向/相邻回归 | **273 passed，54.98s**；无跳过 |
| `python -m compileall -q src tests` | 通过 |
| 本轮四个修改 Python 文件 Ruff | 通过 |
| 两个生产文件反硬编码扫描及新夹具词检查 | 通过；夹具名称未进入生产实现 |
| 文档/状态/链接/章节/空白检查 | 9 份文档、487 个有效相对链接、30 个闭合 Mermaid 块、两版 0–20 章节对应；关闭状态和空白检查通过 |

明确文件集合如下，均在 tests/，新增 19 项已包含在 273 项内，不重复加总：

- `test_skill_retrieval.py`、`test_skill_context_failures.py`、`test_context_input.py`；
- `test_context_runtime.py`、`test_context_request_protocol.py`、`test_history_runtime.py`；
- `test_history_tool.py`、`test_skill_selection.py`、`test_context_index.py`；
- `test_model_retry.py`、`test_product_architecture.py`、`test_product_contract.py`。

测试继续使用独立空临时 cwd、绝对测试路径、独立 cache/basetemp、`-o addopts=`；
全量、L2–L4、Wheel、安装、联网和真实 Provider/API 均未运行。没有 commit、push、tag 或 release。

### 7.3 关闭状态

A-P2-01、A-P2-02 均已修复并完成定向确认；首审的 P0/P1 结论未被改写成新一轮全域独立审查。
当前已登记 Finding 全部关闭，Stop A 完成；F3–F5 仍未开工。
两份上下文的当前阶段、F2 行为、验证、已知边界和计划状态同步更新（第 1、7、15、16、20 节；
正式版第 19 节同步修复状态引用），计划与变更记录同步。架构 owner 和 Mermaid 流程未改变。
