# AO-0：受限策略合同

日期：2026-09-10。依据 ADR-0066 与[统一执行设计](TRACEHARNESS_UNIFIED_EVALUATION_AND_OPTIMIZATION_DESIGN.md)。
本阶段只实现合同、提案校验和停止判定；AO-1 调度器、AO-2 模型分析实现尚不在范围内。

## 1. Owner 与公开入口

- `api/optimization.py` 定义不可变请求、提案、analysis 结果和 typed service。
- `evolution/optimization_contract.py` 冻结源码/实验绑定、开发集身份、可编辑节点及总限额；验证提案并输出原 UE-3 patch；纯函数判断继续、待审或停止。
- 字符串可改范围和 AST 改写只有原 `evaluation/variants.py` 一个 owner；evolution 可以缩小范围，不能扩张。
- `traceh.optimization.strategy@1` 由可信插件经原 `PluginContext.provide` 注册；`traceh.optimization.analysis@1` 由宿主提供，插件 require 借用。
- 服务从当前 Generation Lease 获取；调用在 Lease 内等待结束，之后才释放。插件拥有自身资源，宿主拥有借出的 analysis/Runtime/Store；不互相 dispose。

不改 AgentLoop、评分器、检索算法、权限、工具 schema、Session/Context 协议或默认配置，不增加 evaluator 注册器或第二 PluginManager。
当前公开入口为 Python 合同函数和 SDK，无优化 CLI、后台任务、自动安装、提交、推广或发布。

## 2. 身份、输入与提案

合同 format=1，绑定 experiment_id、base_source_digest、benchmark_digest、run_plan_digest、development_dataset_digest、
允许的 development case IDs、确切可编辑文本与原文 SHA-256、各项显式限额及 UTC 截止时间。
benchmark/plan 摘要绑定原模型、Provider、重试、材料选择、每试次预算和比较条件；原 owner 继续核验原工件。
AO-0 不把调用者提供的摘要当成“已重开原实验”的证明；AO-1 必须调用原冻结/证据读取器后才能构造调度输入。

策略只得到宿主选定的开发案例失败说明、原请求/搜索/读取的定位文字、候选历史、确切允许改动的文本和本次 analysis token 上限。
这些文字是数据，不是新增权限。请求绑定合同、源码、轮次和完整 payload 摘要。
宿主负责选取脱敏内容；本接口不自动识别或清洗任意正文中的秘密。不传 scorer、留出答案、用户会话或万能文件/Runtime 对象。
分析服务只接收同一冻结请求，结果返回 request digest、独立 analysis Session/Turn、原证据摘要及原 Usage；不另造模型循环或计费器。
AO-2 才实现它对原 Runtime/Provider/Budget 的调用，未知 usage 仍未知。

提案携带 request_digest、base_source_digest、rationale、targeted_failure_classes、edits、expected_tradeoffs；或 NoCandidate(reason)。
edits 必须精确匹配宿主提供的 file/selector/old_sha256，拒绝重复节点、空改动、旧基线、错请求、范围外节点和原 UE-3 不接受的文本。
宿主按 file/selector 排序后，以原 UE-3 patch 的 canonical JSON 摘要去重；解释文字和编辑顺序不改变候选身份。
同一候选即使改名或换理由也不能重试取最好值。通过仅代表可交 evaluation，不代表质量提高或允许采用。

## 3. 停止与审阅

限额全部必填且为正整数：max_rounds、max_candidates、max_trials、max_consecutive_no_gain、
max_invalid_proposals、max_duplicate_proposals、max_analysis_calls、max_analysis_tokens、max_request_bytes。
每条试次预算继续由绑定的原 benchmark/plan 负责。总 trial 数包括两臂、失败和已启动后取消的槽位；未启动不伪造成本。
轮次在开始请求策略时记账，候选在校验接纳时记账；单一 AO-1 owner 将先预留后执行，不并发争抢。
decide_next 在开始新轮次之前检查整批预留，不在已获准轮次的每个调用之间重复消耗轮次限额；执行中的取消和单试次预算仍由原 owner 执行。

停止判定消费宿主从既有记录派生的不可变进度，自己不持久化可变状态、不维护第二账本。
取消、截止、违规、未证明收敛/证据、未知分析成本、无新候选，以及达到任一上限都停止。
即将启动的整批 trials 和 analysis reservation 必须装得下，不能截短题目或先运行再发现超限。
尚有 pending_review 时返回 await_review，既不能继续跑新候选，也不能记作收益/无收益；审阅后再判下一步。
此函数不评分，不从 comparison 的总标签猜收益；AO-1 必须核对正式逐题评分、质量非退步、明确成本门槛及完整证据。

本阶段不冻结一个会自动运行的实验实例。开发题 ID、真正未曝光的留出材料、具体试次数和成本需在 AO-1/AO-2 实例中显式给出，
旧 72 题已经曝光，不能作为留出集。没有实例不隐式授权模型调用或沿用机器相关默认值。

## 4. 生命周期与失败边界

可信同进程插件是现有信任边界；接口限权不是防恶意 Python 插件的 OS 隔离，不提前做 S3-B。
策略调用失败或取消必须传播，调用者等原 analysis 关闭后才退出 Lease；重复取消复用原 owned-task 收敛机制。
插件 setup 失败撤销已注册服务；代际切换不提前释放旧 Lease 借用的策略，旧调用完成后再清理。
AO-0 用原 Runtime/PluginManager/Generation 验证这些服务生命周期，不创建新资源管理器。

## 5. 本阶段门禁

实际源码字符串提案经原 AST owner 正向通过；覆盖错请求/旧基线/旧文字/范围外/无效/重复/无改动提案、开发集越界、请求限额，
各停止边界、待审不能继续、未知成本，插件 setup 回滚、代际借用、analysis 失败与重复取消收敛。
关键范围/去重/待审保护做反向验证。运行定向与相邻回归、compileall、collect-only、修改范围 Ruff、文档检查、diff check。
不跑全量 pytest、L2–L4、Wheel、联网或真实模型。本阶段不测候选涨分；真实提案实验属于 AO-2。
