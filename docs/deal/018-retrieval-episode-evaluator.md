# 018：UE-2 独立检索旅程评估器

日期：2026-09-10。UE-2 工程接入与限定真实验证完成；真实答案保持人工待审状态。

## 修改和原因

唯一 EvaluationRunner 静态接入 RetrievalEpisodeEvaluator，复用公共冻结、试次、取消、证据和报告。
四类准备沿原 Runtime/Session、Memory、Plugin/Generation、Tool/Sandbox；没有从 tests 导入生产准备能力。
旧 72 条材料迁为显式 development-regression dataset；不把旧实验脚本当成新协议的第二个运行器。
机器评分核对实际成功 dispatch，人工 review/assess 单独绑定原运行/证据/scorer/rubric；不拿暂定匹配当最终分数。

保留 UE-0/UE-1 的当前协议主线，不新增兼容别名；未重置或删除用户数据，也未修改用户运行配置。
没有提前实现候选比较、优化插件或新的 Runtime 状态机。

## 验证

1. 公共合同、审阅合同、CLI 环境与 Product/Workflow/Promotion 架构集合 122 项通过；明确提供现有 Docker 配置，未依赖跳过来通过。
2. 检索旅程 15 项、公共生命周期 4 项、真实沙箱启动后重复取消 1 项，共 20 项通过；覆盖 History 原文页、Skill 资源、Memory 替代/撤销/跨 Session、Output 搜索与附近读取、非零退出、缺沙箱和重复取消。
3. 后续派发绑定/审阅收口 16 项通过；另新增输出元数据假命中与三个派发反例共 4 项通过。公开 CLI 的检索 run-plan → 原 Runtime → report → review → assess 路径额外 1 项通过。上述分组有重叠，不相加充当独立用例数。
4. 相邻 History/Skill/Memory 搜索 47 项通过；工具输出初轮缺少显式沙箱配置的跳过不算通过，补齐已有 Docker 配置后该文件 33 项全部通过。
5. compileall src/tests 通过；pytest collect-only 3816 项、179 个文件，仅收集；实现与测试修改范围 Ruff 35 个 Python 文件通过；18 份文档的相对链接、两版章节编号、代码围栏和秘密检查通过，git diff --check 通过。生产代码的示例硬编码检查与证据包递归秘密扫描通过。

反向验证在独立测试进程内暂时移除“成功派发”的过滤条件，把真实完成旅程的 Attempt 状态改为 failed 后，
评分器错误地接受原文证据，新增断言按预期失败；恢复过滤后同一组三个绑定测试通过。生产文件未被临时改写。
另有实际执行的反例：输出结果业务值为 7、退出码也是 7，但模型只读第一个字符便回答 7；精确答案匹配为真，
实际正文证据为空，不能联合通过。没有用工具没启动或夹具导入失败冒充反例。

初轮接线失败已修正：测试 Provider 应实现 complete；History 策略从当前持久 policy.config 读取；
eval 的默认 .env 不应误判为审阅动作的显式参数。后者改为仅执行动作填入原默认文件，离线动作先拒绝显式连接参数且不加载环境。
CLI 架构白名单仅增加宿主 review 模块，生产 Runtime 仍不反向依赖 evaluation。

### 真实请求与可复查结果

证据：[概要](../validation-data/unified-evaluation/ue2/summary.json)、[完整证据索引](../validation-data/unified-evaluation/ue2/README.md)。
唯一新 run 为 `b61ef525-435f-4d12-88b1-9a69b639c4b6`，通过现有授权配置加载 qwen-plus；独立进程显式禁用 urllib 代理，
没有打印或保存密钥，没有修改用户配置。只运行种子 113 的八条，不跑基线、全 72 或挑最好结果重试。

| 检查 | 实际结果 |
|---|---|
| 计划/执行 | 8/8，全部执行完成，连接失败 0 |
| 正例证据 | 4/4 正例有回答前实际派发的原文证据 |
| 原记录重放 | 关闭后重新打开 10 个 Session，50 份请求，重放与不变量错误均 0 |
| 作用域 | 越界成功执行 0，两条 Output 各实际执行原命令 1 次 |
| 用量 | 原 Provider 报告 50 次 Attempt，242,709 token；准备 94,048，目标 148,661；未知 Attempt 0 |
| 人工评分 | 8 条仍 pending_review；没有把 Codex 预审当成真人判断导入 |

Codex 对答案和原事件的预审发现三个共性反例：h-absent 只看两页且还有 next_cursor，就断言“之前没有说过”；
s-absent 搜完目录但没有读正文，就断言手册没有规定并扩展到其他事务；m-revoked 只读六条背景事实，就推出项目全局没有有效记录。
这些是模型结论范围问题，不是事实源、Reader 或越权错误；原记录保留，UE-2 不扩展为 Agent 策略优化。
o-absent 搜索完整原输出得到 no-hit，回答限定在该次输出；是否语义通过仍交冻结 rubric 人工审阅。
这不是新的完整 benchmark 得分，不更新历史 55/72。

真实运行后，仅增强离线审阅的原 run 定位及逐条目标事件导出，未改变生成、准备、机器评分逻辑。
原 source.zip、frozen.json 与全部事件都保存在 live-run.zip；离线增强经 CLI 和导入测试确认，不重新调用模型。

## 文档与边界

正式版与通俗版同步 1、3、12.6、13、14、15、17；Product 的历史成功合同保持。
新增 [UE-2 合同](../plan/TRACEHARNESS_UNIFIED_EVALUATION_UE2_CONTRACT.md) 和题库说明。
未运行全量、L2–L4、72 条完整实测、Wheel/发布；未提交或推送。
仅证明单来源隔离旅程，开放多来源选择、UE-3 对照和 AO 自动优化仍待后续。
