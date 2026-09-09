# 简化启动验证

日期：2026-09-08。范围：[ADR-0052](adr/0052-simple-interactive-startup.md)。没有提交或推送，未改用户真实启动配置或密钥文件。

## 改动与主线

- cli/tui_entry 在私有环境中预检查，完整连接直接聊天；缺连接用 tui/onboarding 简短向导，F2/--configure 保留完整设置。工作区和默认存储位置自动确定。
- cli/credentials 用 Windows 当前用户 DPAPI 保存密文，精确绑定服务身份；密码框不回填旧密钥，非密钥 profile/事件不保存明文。不同地址不复用，损坏密文明确失败。环境变量和已有环境文件仍通过原加载器解析。
- tui/session_picker 从真实 Session 记录列出同工作区普通对话，Ctrl+O 或 /sessions 选择、/new 新建；所有切换复用原收尾及恢复。
- chat/workspace_project 重用原 catalog、source resolver、bind_session/CAS 和 MemoryControl。首次人工选择后可记住精确工作区偏好；每个新会话重新证明来源并写正式绑定。取消不绑定；索引失败可以恢复补建，不重复归属事件。
- cli/startup 只读识别旧数据，明确选择后创建独占新目录并保存目标，旧账不迁移、不删除。

## 已验证结果

| 范围 | 结果 |
| --- | --- |
| 最终启动/配置/可选依赖及 Footer 定向组 | 63 passed，40.44s |
| 新功能与原项目归属、真实 Git 定向组 | 38 passed，48.22s；该组执行时新用例尚未增加小屏/取消参数，后续新增由上组覆盖 |
| 治理、Memory、TUI 与 CLI 相邻组 | 中间结果 101 passed / 1 failed，91.16s；唯一失败为 Footer 预期列表遗漏新 Ctrl+O，已同步并在最终 63 项组重新通过；不把中间整组结果记成全绿 |
| 最后向导交互确认 | `tests/test_simple_start.py`：16 passed，8.26s；覆盖服务选择初始为空和小屏可操作性 |
| 编译 | `python -m compileall -q src tests` 通过 |
| 修改范围 Ruff | 通过 |
| 仅收集 | `python -m pytest --collect-only -q`：3418 项，未执行这些用例 |
| 差异 | `git diff --check` 通过；Git 的既有换行转换提示不属于失败 |

最终启动命令：

```powershell
python -m pytest tests/test_simple_start.py tests/test_tui_settings.py tests/test_tui_config_forms.py tests/test_tui_optional.py tests/test_tui.py::test_footer_advertises_only_implemented_global_actions --tb=short
```

项目归属命令：

```powershell
python -m pytest tests/test_simple_start.py tests/test_project_scope.py --tb=short
```

相邻组命令：

```powershell
python -m pytest tests/test_chat_governance.py tests/test_memory_context.py tests/test_memory_context_failures.py tests/test_tui.py tests/test_tui_governance.py tests/test_cli_context_config.py --tb=short
```

## 真实执行与反向证据

首次向导通过 Textual Pilot 实际填写自定义模型与本地服务地址，使用明确标注为 synthetic 的合成密钥；调用 Windows DPAPI 加密保存后，连续两次通过公开 main([]) 启动生产 Runtime/SQLite。第一轮打开向导，第二轮直接聊天；两次都真正经过 HTTP 服务接收请求和 Authorization，得到回复并按原 owner 收尾。不是手工指定 mock 的模型回答来证明配置加载，也没有调用用户真实云服务或收费模型。服务端响应为受控本地夹具，因此不能冒充云模型能力验证。

F2 的本地 HTTP 对照覆盖进程环境、环境文件和已保存密钥三种来源：更换环境文件后读取新的密钥及重试参数，旧 Runtime 已关闭，新请求仍在同一 Session，历史请求可重放。修复了预检查意外固化重试参数的问题，并区分明确输入的临时密钥与解析出的存储密钥，后者不会阻止新环境配置生效。

Windows 密钥反向验证临时从服务身份计算中移除 Base URL：另一地址真实读到了同一合成密钥，测试按预期失败。随后逐字节恢复源文件，最终组中对应测试通过。过程中首次打印反向日志遇到控制台编码错误，源码已由 finally 恢复；用 UTF-8 捕获重跑后取得有效反向结果，没有把打印失败算作验证。

项目测试使用既有生产 Memory fixture 检查新 Session 的正式归属和实际 Context 记忆块；另外使用真实本地 Git 仓库证明合法来源通过、复制仓库拒绝，不创建 managed checkout。注入索引失败后正式关联仍保留，恢复重建索引且不重复追加关联。取消、来源变化、工作区偏好错配、多候选必须选择均已覆盖。

旧协议测试通过生产 Store 写入旧标记夹具，只读诊断后实际点击新空间按钮，检查新 profile 和旧事件逐项不变；Esc 路径不保存选择。会话列表测试检查工作区排除和实际选择后的 RestartChat 目标；80×24 和 110×38 下主要启动按钮保持可见。

## 服务预设来源与边界

服务/地区与模型必须由用户选择，预设仅用于少填地址，不是默认账号或模型，也不代表完成各服务的工具调用兼容性认证。自定义模型与地址仍可填写。2026-09-08 核对的官方资料：

- [OpenAI Chat API](https://platform.openai.com/docs/api-reference/chat/create) 与 [GPT-4.1 模型](https://developers.openai.com/api/docs/models/gpt-4.1)。
- [阿里云百炼地区 Base URL](https://help.aliyun.com/en/model-studio/base-url) 与 [qwen-plus 模型](https://help.aliyun.com/zh/model-studio/qwen-plus)。
- [DeepSeek 首次调用](https://api-docs.deepseek.com/)：当前页面列出 deepseek-v4-flash / deepseek-v4-pro，未使用旧 deepseek-chat 快捷项。

DPAPI 仅支持 Windows 当前用户，其他平台继续用环境文件/变量或临时密钥。加密条目与 profile 的保存不是跨文件事务；保存后一阶段失败可能留下已保存的密文或空的新数据目录。未开启项目能力或没有合格项目时不自动创建/选择项目，不批准 Memory。旧记录不迁移。

未运行全量、L2–L4、Wheel、包索引或发布门禁；未修改 AgentLoop 或 Session/Context 持久协议。

## 文档同步

正式版先改，通俗版随后同步第 1、3、4、13.11、15、16、17 节，替换无条件启动配置页的过时说明、更新模块职责、20 项 profile 字段、权限/生命周期边界与 Mermaid。README、启动配置说明、CHANGELOG 和外部逐行测试说明同步新操作。测试例子不进入通用项目逻辑；服务预设是显式可选产品配置，有官方来源。

文档检查通过：两版 0–20 共 21 个编号章节及 13.11 对应，检查范围的相对链接均存在，代码/Mermaid 围栏闭合。新增流程图按先装配、项目核验、聊天和原 owner 收尾的实际方向绘制。凭据模式检查未发现真实 Key；通用启动与项目代码不含用户测试路径、活动或人员，qwen-plus 仅保留在有官方来源、由用户明确选择的服务模型快捷项中。
