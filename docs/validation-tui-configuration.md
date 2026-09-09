# TUI 配置面板首版定向验证（历史基线）

后续裸命令启动、个人默认与运行中应用见 [当前验证](validation-tui-entry.md)。本文保留首版结果。

日期：2026-09-08。范围：用户授权新增 TUI 配置面板，位于 Stop C 限定验收之后；不扩展检索策略、
插件安装、项目/Memory 权限或 Session 持久协议，不将旧 Stop C 审查报告作为本次新代码的审查证据。

## 结果

- Python 3.12.7、现成 Textual 8.2.8；未安装依赖。
- 下列七个文件最终 `129 passed in 68.01s`，没有 skip。新面板文件包含 18 项检查。
- 启动配置界面实际填写后交给原 `_chat` / `run_tui` / `ChatDriver`，SQLite 中有真正冻结的模型请求
  和 Turn 结束事件；F2 保存另一模型后，当前请求保持原模型，保存的下次启动配置包含新模型。
- 内置 OpenAI-compatible Provider 发出一次本机 HTTP 请求，服务端收到合成密钥 Authorization 和
  指定模型；启动 profile 与原事件账本都没有合成密钥。未访问外部 Provider。
- 密码框不落盘，加载新启动配置清空密码与 Context 草稿；中文路径、空插件选择、CLI 目标覆盖、
  malformed 配置、dotenv 误选、过期文件拒绝、写盘失败保留原文件、取消先于环境加载均通过。
- 80×24 窗口可访问启动/取消按钮；原 TUI 的 Product 审批、取消和退出收敛回归通过。

```powershell
python -m pytest tests/test_tui_settings.py tests/test_tui_optional.py tests/test_cli_context_config.py tests/test_cli_env.py tests/test_cli_chat.py tests/test_tui.py tests/test_tui_governance.py --tb=short --durations=8
```

## 失败与反向验证

初轮测试开发抓到重复模拟点击的 Textual 动画防抖，以及测试读取 Session stream/事件字段用错。
测试改为关闭按钮的视觉防抖效果、使用原 `SessionService.session_stream()` 和 `EventEnvelope.data`，
没有改业务操作或跳过账本验证。

相邻组初轮 `127 passed, 1 failed`：失败是旧 Footer 测试完整枚举未包含新增 F2。
同步新增公开快捷键后，又增加一项加载配置清理临时输入的检查，最终七文件全绿。

临时去掉 `SettingsScreen` 的 Context 加载后文件变化检查，只跑
`test_context_editor_validates_before_showing_and_refuses_stale_save`，它在公开按钮路径写坏了外部
更新的 JSON，并因磁盘内容断言失败。生产文件随后按原字节恢复，最终定向组验证通过。

## 静态检查与未运行门禁

- `python -m compileall -q src tests` 通过。
- 九个相关 Python 文件 Ruff 通过，`git diff --check` 通过。
- `pytest --collect-only -q` 成功收集 3368 项；这是收集，不是全量执行或全量通过。
- 两份上下文同步第 1、4、13.11、15、16 节，使用说明与 CHANGELOG 同步；章节、相对链接和
  Mermaid 围栏核查通过。新增通用模块无本机路径、测试模型或示例身份硬编码。
- 未跑全量、L2–L4、Wheel／安装、真实外部模型或发布级门禁；未提交、推送或发布。

使用方式和仍有的限制见 [配置面板说明](tui-configuration.md)。
