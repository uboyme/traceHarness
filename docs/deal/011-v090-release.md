# v0.9.0：主动检索与上下文容量管理收口

日期：2026-09-10。定位：Educational alpha；本地验收后发布到 GitHub Releases。

## 发布决定

用户在查看 55/72 的结果和剩余限制后明确接受当前能力，授权发版提交并转入沙箱阶段。
因此本次不再把原 66/72 作为发布阻断条件；原冻结题目、评分规则、失败记录和 NO-GO
历史结论不改写。这是接受已知限制的发布决定，不是宣称原质量门槛已经通过。

本次版本号从唯一来源 `traceh.version.__version__` 更新为 `0.9.0`；协议仍为
Session 13 / Context 12，旧 Session 1–12 明确拒绝，保留原数据并使用新数据空间。
没有迁移、清空会话、引入兼容层或采用未获稳定收益的实验策略。

## 交付能力与限制

- Skill 贡献、持久选择、项目批准 Memory、History 原文与主动字面搜索接入原 Reader/Projection。
- 大工具输出保存在原 Effect 账本，支持搜索和有界读取；旧结果折叠、闭合历史压缩、可选语义摘要及完整请求 token 准入共用原请求主线。
- 来源导航、参考交付、当前问题回显及重复拒绝停止保护保持现有边界。
- CLI/TUI 提供启动与功能配置、治理和上下文观察。
- 当前基准为 55/72：History 15/18、Skill 15/18、Memory 14/18、Tool Output 11/18。
  六条直连补测替换原网络失败槽位，基线未重跑；不是新的独立 72 题全量结果。
- 模型仍可能漏读、误认元数据、错误描述证据范围。语义检索关闭；Plugin 为 trusted in-process；尚无 OS 沙箱。

## 验证与发布范围

遵守用户“不跑全量和 L2”的约束：本次只执行版本、当前检索及相邻 owner 定向测试、
全仓收集、compileall、修改范围 Ruff、文档检查、源码包与 Wheel 构建及隔离安装烟测。
不将这些结果称为完整发布门禁或重新独立审查。精确结果见
[发布验证](../validation-v0.9.0.md)。

源码提交、标签和发行包先在本地完成。用户随后明确要求远程发版，本次将 main、v0.9.0 标签、Wheel 与源码 ZIP 发布到现有 GitHub 仓库 Releases；不上传 PyPI。实际发布状态以 [GitHub Release](https://github.com/uboyme/traceHarness/releases/tag/v0.9.0) 为准。发布提交使用 `[skip ci]`，遵守本轮不运行全量/L2 的约束。
原始实验数据库、事件导出及隔离源码保留本地；Git 收录报告、判定和校验清单，
不提交缓存、活动 SQLite 文件和用户无关笔记。原始证据保留策略见
[证据目录说明](../validation-data/active-retrieval/README.md)。

## 下一阶段

进入 [v0.10 S0 沙箱启动计划](../plan/TRACEHARNESS_V0.10_SANDBOX_S0.md)：先验证真实后端
能力与威胁边界，再冻结执行合同。当前提交不提前实现 Sandbox executor、MCP 或 isolated Plugin。
