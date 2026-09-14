# 百炼模型切换配置

2026-09-13，用户要求先换一个百炼支持的模型，并举例 DeepSeek V4.1 Flash。

查询百炼官方 [DeepSeek API](https://help.aliyun.com/zh/model-studio/deepseek-api) 与 [V4 Flash 模型页](https://help.aliyun.com/zh/model-studio/deepseek-v4-flash)，可以确认 `deepseek-v4-flash` 支持 OpenAI 兼容接口及 Function Calling；尚未从百炼官方目录确认 `deepseek-v4.1-flash`，不将 V4 与 V4.1 混称。另有 `deepseek-v4-pro`、`deepseek-v4-flash-0731`，以及独立的 [qwen3-coder-plus](https://help.aliyun.com/zh/model-studio/qwen3-coder-plus) 可作为后续候选；目录可用不证明当前账号和地区已开通。

本次仅将当前项目启动配置 `.traceh-tui.json` 的 `launch.model` 从空白继承改为明确的 `deepseek-v4-flash`。其他 JSON 字段逐项核对未变化；个人默认、连接、密钥、权限、预算、生产源码和历史验收材料均未修改。原严格 profile reader 和 initial_settings 解析确认选择生效。保存配置不更改正在运行的 Runtime，下一次使用该 profile 启动才采用新选择；现有已确认 Product Profile 仍遵守原模型绑定校验。

百炼文档说明 V4 默认启用思考模式；本次未添加思考参数、修改 Provider 或承诺思考工具多轮兼容已验证。没有调用模型、查询账号模型列表或恢复 WC-4 目标，不能把配置保存当作连接成功或新验收通过。当前驱动使用同一选定 Provider/model 供主方与助手调用，不是只切主方的独立模型实验。

本地模型选择不是软件通用默认。正式/通俗上下文同步 13.11；WC-4 最新真实结果仍是记录 057 的失败，新增真实运行必须另行冻结与授权。不提交、推送或发版。

验证：启动配置与个人继承相关定向测试 13 项通过，受影响两测试文件 collect-only 通过；compileall、git diff --check、两版章节/链接/代码块和秘密模式检查通过。没有生产 Python 修改，未新增 Ruff 检查范围；未跑全量或 L2。
