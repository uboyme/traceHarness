# UE-4 显式真实验收驱动

这些脚本只用于 [UE-4 合同](../../docs/plan/TRACEHARNESS_UNIFIED_EVALUATION_UE4_CONTRACT.md) 的显式实验，
不自动运行、不加载为生产 evaluator，也不新增评分器或优化循环。需要已授权的模型连接与现有固定 Docker 镜像。
普通 pytest 不会调用真实 API；`test_ue4_controls.py` 使用确定性 Provider 替身，真实 Docker 子集要显式配置镜像和 context。

- `baseline.py`：经原配置/凭据加载器使用明确 profile，单进程空 ProxyHandler 直连；同一个 EvaluationRunner 跑原 72 条，
  current 单臂、每题一次、无重试、模型超时 60 秒、总运行期限 7200 秒。打印的进度仅转发原 execute 结果。
- `controls.py` + `controls.json`：四条普通问题和四条来源竞争，各自新 Runtime/Session/Store。
  原 Memory 服务真批准、Skill 经原插件装配、History 真对话后压缩、工具输出经原 Sandbox 真实执行一次。
  四类 reference 工具同时可用；目标轮不允许工作区探索或重跑 shell，因此它是受控多来源实验，不代表开放 Chat 的全部权限配置。
  此测试驱动复用原构造器/资源关闭和证据检查，没有扩大生产 source-isolated evaluator 的合同。
- `reopen.py`：只读关闭后的原 run，重开所有有证据的 Session，运行原请求重放及不变量检查，再调用原 --review 实现导出审阅包。
  确认前后原文件摘要相同，正例暂定联合计数与负例待审分开。没有 Provider 调用或人工评分导入。

以下是路径占位示例，须由使用者显式替换，不是系统默认。PowerShell 启用 UTF-8；controls 的模块根为本仓库 tests：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Resolve-Path .\tests).Path
python .\tests\live_unified_evaluation\baseline.py --profile <profile文件> --sandbox <沙箱文件> --benchmark .\benchmarks\retrieval_episodes_v1 --output <新的基准输出目录>
python -m live_unified_evaluation.controls --profile <profile文件> --sandbox <沙箱文件> --benchmark .\benchmarks\retrieval_episodes_v1 --materials .\tests\live_unified_evaluation\controls.json --output <新的对照输出目录>
python .\tests\live_unified_evaluation\reopen.py --run <基准输出目录>\run --output <新的离线复核目录>
```

72 条结果、8 条辅助对照及历史 UE-3 A/A 波动分开报告，不拼成一个“最佳成绩”。没有候选就不跑第二臂。
`controls.json` 中的项目、核验码、问题与脚本是显式测试材料；不在通用源码里成为默认值。
宿主材料含答案，只保存在实验输出根；模型只获得原来源内容与当前问题，不获得答案清单或评分报告。
秘密只由原加载器在进程内使用，输出归档前应检查真实凭据字节，不能保存原 .env 或密钥。
