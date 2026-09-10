# UE-3+：原真实旅程的离线诊断

这批数据分析的是 [UE-3 原实验](../ue3/README.md)，不是新的模型成绩。
原 run 为 `366e729d-c6c4-4f08-a50a-8ecb4cc8b211`；八题两臂，共 16 条真实 qwen-plus 旅程。
本轮使用当前分析器只读原 SQLite/请求/证据，新增 Provider 调用为 0；原实验 719 个文件逐个字节摘要前后一致。

| 观测 | aa-reference | aa-repeat |
|---|---:|---:|
| 完整保留的旅程 | 8 | 8 |
| 正例来源候选已出现 | 4/4 | 4/4 |
| 正例足够证据已进入请求 | 4/4 | 4/4 |
| 负例（正向指标不适用，范围待审） | 4 | 4 |
| 原人工评估状态仍 pending_review | 8 | 8 |

“来源候选”是来源容器粒度：出现正确 Skill 目录也算找到来源，但不算读到正文。
“派发”只证明足够证据进入成功请求，不证明模型理解了；原人工评分与暂定答案匹配分开保留。
这不是 Recall@K、消费率或新的质量得分，不能据此把原比较的 inconclusive 改成胜出。

## 两个能直接看懂的例子

- `o-direct` 问旧设备检查输出的校验码。两臂都是列出旧输出、搜索“交接校验码”，没有额外 `read_tool_output`；
  搜索片段已经包含并通过原 Effect/正文校验，因此证据为 observed。不能因为少调用一次 read 就判失败。
- `h-absent` 问历史里是否提过保险柜口令。第一臂读页 0、1、2，第二臂读页 0、1，仍有 next_cursor。
  诊断保留这些实际范围和原回答，供人核对“检查了所有页面”一类表述；不会把读了几页或没命中自动变成全库不存在。

另一个 `s-absent` 的第一臂实际读了 section-23，并在导航的 id/title/summary/tags 中搜索“车辆”，扫描 32/32、无命中；
第二臂读同一章节但没有搜索。原回答把短章节文字称为摘要，并依据主题推断手册不存在该内容。
诊断照实保留实际 tier=section 和导航搜索范围，不根据模型自己的叙述改写执行事实，也不直接自动判分。
这些名称和数字都是明确实验材料，不是系统默认。

## 文件与复核

- [summary.json](summary.json)：全部 16 条原问题/回答、评估状态、来源/派发定位、读取状态及查询范围。
- [baseline-diagnostics.md](baseline-diagnostics.md)、[candidate-diagnostics.md](candidate-diagnostics.md)：可读表格与完整 JSON；
  对应 [baseline-diagnostics.json](baseline-diagnostics.json)、[candidate-diagnostics.json](candidate-diagnostics.json)。
- [analyzer-source.zip](analyzer-source.zip)：本轮分析器使用的完整 Python 源码字节，和原被测源码明确分开。
- [artifacts.json](artifacts.json)：本轮产物摘要、分析器 source_digest、原实验归档引用与摘要；不重复保存原 23.5 MB 实验。

原事件和请求仍在 [UE-3 live-experiment.zip](../ue3/live-experiment.zip)。复核时分别解压原实验和分析器到新的目录，
在已具备项目依赖的环境把 PYTHONPATH 指向分析器源码根（其下有 traceh），再执行以下示例命令：

```text
python -m traceh.cli.main eval --review <原实验解压目录>/arms/01/run --output <新的原版审阅目录>
python -m traceh.cli.main eval --review <原实验解压目录>/arms/02/run --output <新的重跑版审阅目录>
```

目录占位符需由使用者显式替换；两次导出的 diagnostics.json 应分别与本目录 JSON 完全一致。
命令不会加载模型连接、执行工具或重跑旅程。分析器源码快照不是依赖锁，环境身份继续参考原 UE-3 归档。
原始待审状态保持不动，人工判断仍须走原 --assess 和明确 judgment；没有代替用户填写真实人工评分。
