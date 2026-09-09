# Typed reference Skill examples

独立示例包，两个 `traceh.plugins` Entry Point：`traceh.reference.current` 和
`traceh.reference.retired`。每个只注册一项内联 typed Skill，包含 descriptor、摘要、section
SHA-256 和实际 bytes；不注册 Tool、Prompt、Provider 或后台任务。安装不会自动启用。
它与旧 `traceh-example-skill-plugin` 的 Prompt/Tool 演示是不同的明确示例。

宿主必须显式提供 `SkillPolicy`、Context policy 并启用相应插件，再持久选择 Skill。
示例版本 `0.1.0`，使用核心 `traceharness-py>=0.8,<1.0`，Python >=3.12。
退役、注册回滚和资源释放均使用既有 Activation/Generation/Lease/Drain，不实现自己的生命周期。

[冻结检索基线](../../../benchmarks/retrieval_v1/README.md) 用旧项实际启用后退役的方式验收代际隔离。
本轮只从源码通过原 PluginDiscovery/PluginManager 做定向验证；没有构建或安装 Wheel。

此示例使用当前开发树的 typed Skill SDK；已发布 0.8.0 不包含该 SDK，不能把版本依赖范围当作安装验收证据。正式兼容范围随发布候选门禁核对。


导航合同要求每个 section/resource/chunk 显式提供非空 title 和 summary；这些字段进入原目录摘要。
本示例只有一个章节，构造时显式使用调用者传入的说明作为该章节标题与说明；这是示例数据安排，
核心不会从正文或 ID 推断。旧贡献缺字段会拒绝，已记录旧协议的数据须使用新数据目录。
