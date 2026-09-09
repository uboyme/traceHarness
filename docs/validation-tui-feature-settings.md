# 中文启动与功能配置：定向验证

日期：2026-09-08。范围：启动先显示配置页，中文 Context/Product 结构化草稿、功能开关、已安装插件
选择、自动压缩表单。保留原 Runtime/Session/审批边界；没有新增检索策略或持久事件协议。

## 已验证事实

- 使用已安装 Python 3.12.7 / Textual 8.2.8，无安装或真实外部模型调用。
- 以下 10 文件共 181 项定向覆盖。初轮 `180 passed, 1 failed in 102.30s`，唯一失败是高级折叠区
  滚动动画与测试鼠标坐标竞争。该项改用公开焦点/Enter 操作后，配置两文件 `36 passed in 29.32s`；
  其余 8 文件的 145 项已通过。未把初轮失败隐去或报告成单轮全绿。
- 从空配置通过真实 Textual 按钮/Tree/输入框开启 History、Skill、Memory，添加来源与资源根，保存后
  由生产 Context 解析器读取；关闭总开关返回的启动参数不装配 Context，原文件保留。
- 从新 Product 草稿填写审批署名、仓库/分支、模式和逐个 argv 参数；缺失必填项拒绝保存，填写后
  由生产 Product 解析器读取。配置阶段不执行命令、不创建仓库。现有 Product 审批与取消 TUI 回归通过。
- 插件从安装元数据选择，启用/关闭结果写入启动输入；没有在配置列表中导入插件代码。
- 已有值加载、临时密钥不落盘、误选合成 dotenv 不显示内容、取消、非法预算、观察到的文件变化、
  关闭后重新开启、格式 1 可选压缩字段及显式关闭覆盖环境阈值均通过。
- 裸命令先显示表单且尚未创建 Store；原 loopback HTTP Provider 与 SQLite 主线确认 F2 应用会收尾
  旧 Runtime、恢复同一个 Session，并保留此前请求证据。未调用真实外部服务或真实 Key。

```powershell
python -m pytest tests/test_tui_config_forms.py tests/test_tui_settings.py tests/test_product_config.py tests/test_cli_context_config.py tests/test_cli_env.py tests/test_cli_chat.py tests/test_tui_optional.py tests/test_tui.py tests/test_tui_governance.py tests/test_product_architecture.py --tb=short --durations=8
python -m pytest tests/test_tui_settings.py tests/test_tui_config_forms.py --tb=short --durations=3
```

## 反向验证

临时移除 `ConfigForm` 保存前的 `validate_document()`，只执行
`test_form_cancel_invalid_and_stale_preserve_files[invalid]`：非法负数页大小被保存，界面错误地返回设置页，
测试因应停留在草稿页的断言失败（`1 failed in 2.32s`）。生产文件按原字节恢复后，取消/非法/过期
文件三个公开路径重新通过。没有用未发生的操作冒充验证。

实现期间也捕获并修复了 Textual 8 空选择常量变化；改用公开 `clear()` / `is_blank()` 操作。

## 静态检查与边界

- `python -m compileall -q src tests`、8 个相关 Python 文件 Ruff、`git diff --check`。
- `pytest --collect-only -q` 是收集检查，没有执行全量测试。
- 两份上下文同步 1、4、13.11、15、16 节及启动流程图；使用指南、README、CHANGELOG 和外部体验
  手册的启动步骤同步。检查章节对应、相对链接、Mermaid 围栏及新增示例/秘密边界。
- 预设是显式界面草稿，不是 Runtime 缺省；Git 项目/目标、插件身份、审批人和验证命令由用户填写。
  配置解析通过不能代替认证、插件激活、真实 Git 身份/目录校验或发布验收。
- 没有全量执行、L2–L4、Wheel/安装、联网包索引、外部模型、提交或推送。

使用步骤见 [中文配置指南](tui-configuration.md)。
