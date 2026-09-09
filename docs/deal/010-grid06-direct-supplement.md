# grid-06 网络失败题直连补测

日期：2026-09-10。用户授权只复测原候选的六条网络失败题，不跑基线，并将新通过题计入总分。

**当前成绩：55/72（76.4%）＝原 51 条通过＋本次 4 条通过。** 六条均直连完成，无最终连接失败；原始运行记录保留，最新成绩用补测结果替换这六个失败槽位。这不是重新执行了全部 72 题。

| 用例 | 结果 |
|---|---|
| 113-h-english：历史临时交接代号 | 通过，成功派发的历史原文支持答案 |
| 113-s-english：Skill 签到卡代号 | 通过，读到对应章节正文 |
| 227-h-absent：历史是否提过保险柜口令 | 未通过联合标准：完整字面搜索扫过 20/20 条记录，但回答错误声称历史只有两条；结论方向正确，依据描述不一致 |
| 227-h-disambiguate：区分复核组与搬运组 | 通过，身份与代号匹配 |
| 419-h-disambiguate：另一组消歧材料 | 通过，身份与代号匹配 |
| 419-o-nonzero：失败检查的故障代号 | 未通过：原输出有 `REF-7601D3A484`，模型没有找回，却回答不存在故障代号 |

保持原题目、材料、模型、预算与当前生产源码。只在独立实验进程禁用代理并恢复 opener，没有修改用户系统代理或生产连接设置；没有基线调用，没有重跑到满意。

六条记录都完成实际请求重放和不变量检查，原工具检查命令只执行一次。原来的 15 条行为失败不动，加上本次 2 条回答/证据问题，当前剩余 17 条未通过。原 66/72 门槛及发布 NO-GO 不变。

证据：[冻结配置](../validation-data/active-retrieval/grid06-direct-supplement/frozen.json)、[逐题人工判定](../validation-data/active-retrieval/grid06-direct-supplement/manual-review.json)、[最新 72 槽位成绩](../validation-data/active-retrieval/grid06-direct-supplement/current-score.json)、[请求和调用摘录](../validation-data/active-retrieval/grid06-direct-supplement/review-packets.json)、[独立重放](../validation-data/active-retrieval/grid06-direct-supplement/candidate-reopen.json)。

本次未修改生产代码、未跑全量测试或 L2、未提交或发布。两份项目上下文第 1、7.11 节及验收页同步最新计分。
