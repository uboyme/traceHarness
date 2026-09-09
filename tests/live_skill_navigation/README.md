# 显式真实 Skill 导航回归

这是开发用真实 Provider 集成测试，直接调用生产 Runtime、PluginManager、SQLite、Context 和 Tool。
不是新的产品评测入口，不实现 ProductTask/Benchmark 生命周期，不替代唯一 `traceh eval`。
`run.py` 不会被 pytest 自动收集；运行必须明确提供模型环境、输出目录、重复数、入口层级与调用上限。

```powershell
# 示例：MODEL_ID 必须替换为调用者确认可用的模型；不是系统默认。
python tests/live_skill_navigation/run.py --env-file .env --model MODEL_ID `
  --output PATH_TO_NEW_OUTPUT --corpus tests/live_skill_navigation/corpus.json `
  --repeats 3 --tiers directory summary --max-steps 12 `
  --max-output-tokens 16384 --timeout-seconds 300
```

也可省略 `--model`，明确使用原 `TRACEH_MODEL` 配置。凭据只由原 CLI 环境加载器交给原 Provider，
不会进入报告或模型提示。所有目录与操作参数均为测试输入；资源放在空模型工作区之外，宿主 Policy
只允许 `request_skill_reference`，其他工具调用会被拒绝并计为失败。

语料均为明确标注的合成测试材料：三个 Skill（其中一个未选中）、相似章节、资源分块、中英文四类任务。
重复组通过固定规则改变无语义 ID；相同组在不同模型或对照条件下保持相同 ID。模型只收到自然语言任务、
生产检索得到的参考与生产工具合同，不提前获知 Skill/章节 ID、正确答案、指定工具调用或评判规则。
临时源码插件使用标准库 distribution metadata 经原 Entry Point/激活入口进入 Runtime；没有构建或安装
Wheel，因此此脚本不证明打包能力，不属于 L2。

每次记录 SQLite 事件、冻结请求、工具调用、披露正文、答案、重放/不变量结果、耗时及可用 token 计量。
`manifest.json` 绑定语料、测试脚本、导入的生产源码摘要和模型/调用配置。
`passed` 是原严格导航指标：答案包含所需事实、正文确实披露、没有多余正文读取或越权/工具错误，且可重放。
`task_passed` 另报有证据支持的任务完成情况，允许读取额外正文；不会把严格失败改写成严格成功。
摘要和目录本身不含答案；仅猜对答案、扫遍全部正文都不能通过严格指标。

失败样本保留，不删去后重算成功率。Provider 失败单独记录稳定 category/code；重复补测不能替代原失败。
这些有限样本不证明所有模型或任意任务都可靠。当前实测与边界见
[验证记录](../../docs/validation-v0.9-skill-navigation.md)。
