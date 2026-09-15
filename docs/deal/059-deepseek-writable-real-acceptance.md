# WC-4 DeepSeek V4 Flash 有界真实测试

2026-09-13，用户在模型配置切换后明确授权“直接开始测试一波”。复用原 writable_acceptance / Evaluation / Product 主线，新目录 `.traceh/wc4-deepseek-20260913/`；主方和助手均配置 `deepseek-v4-flash`，但实际未进入助手阶段。

## 冻结与结果

一题一次、主子最多 32 次调用、600 秒、连接 60 秒、零重试。原材料生成器、初始文件、权限、参考解与检查逻辑未修改；调用前冻结新材料，预检占位实现 exit 1、参考实现 exit 0。与第三轮比对发现生成的 Python 集合字面量两文件的排列顺序不同，导致 dataset/验证器字节摘要不同，检查语义相同；不能声称跨轮字节完全相等。本轮冻结后未修改它，且失败发生于读规格/分工之前，不是这项差异引起。

| 项目 | 实际证据 |
|---|---|
| 模型 | deepseek-v4-flash，保留既有连接，未改思考或 Provider 参数 |
| 调用 / tokens / 耗时 | 3 次主方、0 次助手；5523 exact tokens；16922 ms |
| Provider 失败 / 重试 | 0 / 0；三次响应成功，不是网络失败 |
| 第一步 | list_files 执行成功 |
| 第二步 | 一次请求三个 read_file，违反 SCOUT 每步最多一个调用；整批拒绝，三个文件均未读取 |
| 第三步 | 冻结请求只暴露 submit_collaboration_plan，模型仍返回三个 read_file，且参数名改为 file_path；整批拒绝 |
| 终态 | CollaborationPlanInvalid，Product/Workflow failed；无助手、Patch、固定完成检查、Review、Approval 或 Promotion |
| 资源 | 2 账户 closed、4 使用预留 settled；2 工作区 released、live 0 |
| 副本重放 | 5 份请求通过，3 真实加 2 脚本控制；原数据库不变 |

Session 的 7 个 tool/call 表示模型提出的调用，只有 1 次真正执行，另外 6 次由冻结 Step view 拒绝，不能将 7 当执行成功数。驱动 execution.stopped=false 表示 Provider 包装器没有触发调用上限/传输失败熔断；原 Runner 已正常返回失败报告，不表示任务仍在运行。

## 判断与边界

原冻结请求 seq 10/22 仅允许 list_files、read_file、search_text，提示每响应最多一个调用；seq 37 仅允许 submit_collaboration_plan。原 Provider 按当前 request.tools 序列化，不从历史消息补回读文件权限，也未发送强制 tool_choice 或 parallel_tool_calls 限制。本轮能证明模型在当前适配和阶段提示下不遵守工具限制，不能证明 DeepSeek 编码能力不如 Qwen，也不能声称已经验证了真实助手或修复能力。

通俗说：新主方还没开始写作业，就在“一次只取一本书”的步骤取了三本，随后轮到填写分工表，它仍想取书。门禁正确拒绝，试验提前结束。是否需要将阶段约束同时表达为 Provider 支持的调用参数，应另行核查能力与合同，不能本轮放宽权限或自动追跑。

没有生产源码修改、全量/L2、安装、镜像拉取、额外模型调用或项目提交。已有未提交改动与前三轮证据保留；两版上下文同步 13.11、14.3.8、15 和当前状态，入口/导航/计划/交接同步本次结果。WC-2/3 完成，WC-4 仍未通过。

分享证据见 [summary.json](../validation-data/dynamic-collaboration/writable-deepseek/summary.json)。原数据库 SHA-256：`59bfc34eeadc58418ca00a31a81c3a91a16d8b584ef4dae6fc3679945e66e6b2`。

冻结源码/驱动/材料复核、两版章节对应、相对链接、代码块闭合、秘密模式和 git diff --check 通过；四轮原数据库摘要均未改变。没有源码修改，未重复运行既有源码门禁。
