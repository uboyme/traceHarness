# WC-4：有界真实可写协作验收未通过

2026-09-13。WC-2/3 实现与定向验收已完成；WC-4 接入原 Evaluation，并按用户授权执行唯一真实题。本轮未达到完整验收，按冻结规则停止，不追加模型调用、不改题或放宽评分。目标不能标记全部完成。

## 实现与冻结

原 ProductTaskEvaluator 的 handoff 读取按 Directory 实际 apply_patch 授权区分 patch_author / investigator；可写输入核对 task、owner、child、session、message、Workspace 和 source，再沿原报告与请求快照归属。原整树执行费用和检索角色归属支持 patch_author，继续使用原 investigations 报告容器，不新增运行器或事实源。示例 benchmark 配置增加显式角色槽后，其 run-plan 摘要同步当前材料字节。

`tests/live_dynamic_collaboration/writable_acceptance.py` 与 `writable_materials.py` 冻结一个遥测清洗/汇总题：助手修改 telemetry_rules.py，主方修改 telemetry_report.py；只准修改两文件，不新增文件或依赖。固定检查覆盖输入类型、边界、统计、顺序和原输入不变，同时校验文件集合及规格摘要。题目内显式 .gitattributes 禁用文本转换，未改变生产字节规则。

冻结主子合计最多 32 次模型调用、总时限 600 秒、单连接 60 秒、零重试、一题一次；首个 Provider 失败、取消或封顶拒绝后续调用。started.json 排他创建禁止重跑同目录。使用已有连接及镜像，Docker 预检不拉取：占位实现 exit 1，参考实现 exit 0。固定隔离 benchmark 批准策略不授予项目仓库提交或发版权限。

## 真实证据

| 项目 | 结果 |
|---|---|
| 真实调用 | 18：主方 13、助手 5 |
| Exact tokens | 124609：主方 98686、助手 25923；无 unknown usage |
| 工具调用 | 15：主方 11、助手 4 |
| 耗时 | 171406 ms，约 171 秒 |
| Provider 失败 / 重试 | 0 / 0 |
| 子 Patch | 1757 字节，仅 telemetry_rules.py；原 CAS 摘要核验通过 |
| 原文读取 | 全文 0–1757，进入主方后续 9 份实际请求 |
| 整合 | 原 Tool Effect 回执 applied；最终 Artifact 保留同一 rules 改动 |
| 固定验证 | functional-contract exit 1；Product / Workflow failed / failed |
| 外部推广 | 无 Promotion，隔离目标仍为初始 revision |
| 收敛 | 3/3 账户 closed，20/20 使用预留 settled；live 0，released 1、quarantined 2 |
| 重放 | 原数据库副本 20 份请求通过：18 真实、2 脚本控制；原文件摘要不变 |

主方确实新增并运行 `python test_telemetry.py`，原 shell 回执 exit 0。但题目明确禁止新增文件，最终 Artifact 包含这个额外文件，固定文件集合检查拒绝交付。测试脚本主要打印返回值，关键反例没有断言失败出口，exit 0 不证明完整功能合同。最终报告却声称没有新增文件、全部符合规格，与原工具和 Artifact 矛盾。

助手 Patch 有实际实现并被下游调用，但不完全正确：缺少顶层 rows 的 list 校验。对已核验 Patch 的独立离线诊断观察到 normalize(None) 抛 TypeError，normalize({}) 和 normalize(()) 返回 []；规格均要求 ValueError。最终主方没有修复。该诊断没有修改运行证据或重跑模型，也不把失败结果改记为通过。

因此分别判断：**协作写入/原文交接/显式整合机制成立；完整功能验收失败；语义质量存在已证实缺陷；收益未测量。** 声称 Production-ready 或全部通过不是事实。失败工作区按原规则隔离保留，不手工清理冒充全部 released。

## 定向门禁与交付边界

WC-4 新增原 Evaluation 可写主线及原文归属回归 2 passed；冻结与相邻检索诊断集合 21 passed、4 skipped（该次未配置沙箱）；配置沙箱的架构/合同/相邻 Evaluation 集合 40 passed、2 个旧摘要失败，摘要修正后合同与冻结重跑 39 passed。最终架构/合同/冻结集合 56 passed。集合重叠，不相加。WC-2/3 定向与反向证据分别见记录 052/053。

compileall、修改范围 Ruff、collect-only 4183、git diff --check 通过。未运行全量、L2–L4、Wheel、安装、基线、额外模型裁判或第二轮真实验证；没有提交、推送、tag 或发版。

两版上下文按正式先行同步第 1、12.6、14.3.4、15、16 节，入口、导航及 WC 计划更新真实失败状态。仍待解决的是模型漏验顶层类型、违反文件交付约束及报告真实性；修复与再次真实验证需要另行授权，不突破本轮一次上限。

原证据位于 `.traceh/wc4-writable-20260913/`；分享摘要见 [summary.json](../validation-data/dynamic-collaboration/writable/summary.json)。原 WC-1G 记录 051 及此前失败全部保留。
