# TUI 直接文字复制与 Memory 表单定向验证

本次修改 TUI 展示、文字选择和系统剪贴板入口。治理写入仍由原 ChatGovernance、Memory owner 与事件
账本负责。使用已安装的 Textual 8.2.8 和 Anaconda Python 3.12.7；没有新增安装、真实模型调用或用户
Memory 写入。

## 已验证事实

- 聊天正文直接拖选、高亮、Ctrl+C 复制、右键打开“复制”并粘贴实际通过。测试使用 Textual Pilot
  派发鼠标事件，在长对话滚动后定位包含中文和 emoji 的内容，核对选区和复制结果；没有打开独立复制页。
- 没有选区时 Ctrl+C 不退出，右键菜单的复制按钮禁用；Esc 关闭菜单。治理证据和输入框复制也通过。
- 顶部按钮栏与 F3 复制页已删除。没有 Product 配置时聊天占满宽度；已配置 Product 的原操作与退出
  收敛测试保持通过，退出改用 Ctrl+Q。
- 真实 Windows Unicode 系统剪贴板通过原生接口完成合成中文、emoji 和换行的写入、读回核对。没有
  读取此前的用户剪贴板。应用正常运行时使用此接口，headless 测试不写系统剪贴板。
- 复制期间没有新 Session 事件或模型请求。F4 或 `/memory` 仍从真实记录选择提议与 active ID；
  缺少署名或选择时停留表单并提示；填写草稿不写 Memory。
- 批准、替换、撤销经原人工 CONFIRM；取消保留原 active，替换保留槽位；关闭表单或确认时仍等待
  原 owner 收敛。
- 反向验证临时移除渲染行的原文坐标：同一鼠标拖选测试选中了错误范围，在选区内容断言失败，证明
  测试确实覆盖主界面选择。随后按原字节恢复源码。本机临时日志：`traceh-tui-direct-copy-reverse.log`。

以下七个文件最终 **152 passed in 84.65s**，包含直接选择、治理、原 TUI 布局和退出、设置与可选安装、
共享治理、架构与协议拒绝回归：

```powershell
python -m pytest tests/test_tui_governance.py tests/test_tui.py tests/test_tui_settings.py tests/test_tui_optional.py tests/test_chat_governance.py tests/test_product_architecture.py tests/test_context_request_protocol.py --tb=short -o addopts= -q
```

同范围 collect-only 为 152 项。`compileall -q src tests`、六个受影响 Python 文件 Ruff、diff-check 通过。
JUnit 记录在本机临时目录 `traceh-tui-direct-copy-final.xml`。生产实现没有案例、操作人或槽位的隐藏
默认值；新记忆 ID 留空时生成 UUID。

## 任务面板展开/收起补充验证

Ctrl+B（Footer“任务面板”）在聊天主界面切换原 Product 控件显示，默认按本次 Product 是否配置决定
展开或收起。未配置也可展开查看提示；这只是显示开关，不启用 Product、不停止工作、不新增持久状态。
全屏详情/模态页和退出收尾期间不切换底层面板。审批草稿保留，重新展开恢复确认框焦点，仍需原人工确认。

五个文件最终 **110 passed in 97.62s**：

```powershell
python -m pytest tests/test_tui.py tests/test_tui_governance.py tests/test_tui_settings.py tests/test_tui_optional.py tests/test_product_architecture.py -o addopts= -q --tb=short
```

覆盖反复切换、未配置提示、宽窄屏、展开后的聊天换行、模态页无操作、隐藏确认框时 Enter 不执行任务、
展开后草稿/焦点恢复及取消；隐藏期间原观察者继续读取真实 SQLite 的变更，展开显示最新失败状态。
切换没有新增模型请求或 Session 事件。临时移除 Ctrl+B 绑定时，公开键盘测试因面板无法展开而失败；
随后恢复原源码。临时目录证据为 `traceh-tui-panel-reverse.log` 和 `traceh-tui-panel-final.xml`。

同范围 collect-only 110 项，compileall、两个修改 Python 文件 Ruff、diff-check 和文档 QA 通过。
反示例扫描只命中既有 `ChatDriver` 类型名；没有案例默认值。未跑全量、L2、真实模型或发布门禁。
该显示偏好不跨重启保存，已运行的旧 TUI 需要退出重启才会加载新快捷键。

## 尚未运行与边界

未运行全量、L2、真实模型 API、发布安装门禁或独立多 Agent 审查。鼠标路径采用真实控件的 headless
事件验证，系统剪贴板另做本机原生读写验证，未声称自动操纵用户正在运行的终端。Windows 剪贴板被其他
应用占用时会提示失败，所选文字仍可在 TUI 内粘贴；其他系统保留 Textual OSC 52，取决于终端支持。
复制内容来自当前最多 2,000 行渲染文本，不是完整会话导出。Memory 表单不自动批准、不自动绑定项目、
不自动重建检索索引。已经启动的旧 TUI 需要完全退出并重新启动才能载入修改后的 Python 代码。
