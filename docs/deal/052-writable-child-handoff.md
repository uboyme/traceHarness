# 052：WC-2 受控可写助手交接

2026-09-13。用户授权按交接顺序继续 WC-2～WC-4；本记录只覆盖 WC-2。保留既有未提交改动，未提交、推送或发版。

## 范围与结果

显式 patch_author 配置开启一个独立受管可写助手；null 保留只读调查。Product/host 6、Product event 5，旧格式拒绝。任务工作包 format 1 绑定主子、消息、源码版本、装配/预算摘要、职责和交付路径。原 Supervisor、Inbox/Delivery、Budget、Workspace、CaptureGate、Artifact/CAS 负责执行、捕获和留证，无新增调度或事实账本。尚无整合工具。

公开 Product 正向用例中，助手修改自己 tracked.txt，主方收到真实产物摘要，但主方同名文件仍是 base。重复收集不再捕获，工作区释放后仍能读取同一证据。Provider 失败、越界交付和错误身份/工作版本不会成为成功交接；原请求副本可重放。测试中的批准只操作临时隔离目标。

重复取消发现原 Supervisor 的关闭重入死锁：祖先关闭等待主方，主方清理又排队关闭已被祖先收敛的助手。原 owner 现在仅在准入封闭且静止、对应 cleanup Task 已存在时复用该 Task，包括失败结果。正常恢复会移除旧 Task，不跨实例代际猜测。未修改 AgentLoop、AgentRuntime 或 PluginManager。

## 可重复证据

- `test_writable_collaboration.py`：11 项覆盖隔离/真实 Capture、Provider 失败、越界、六组信封绑定/版本拒绝、权限、释放后重复读取和重复取消。
- Product/Supervisor/Budget/Workspace/配置/TUI/协作定向集合：首次 319 passed、2 failed；两项为原 Supervisor 文件摘要保护，依据通用修复更新保护摘要后，相关 Product 合同/架构/版本/生命周期集合重新运行 150 passed。
- 原 Capture/Artifact/Workspace 架构及 TUI 相邻集合：67 passed。
- 生命周期集合 24 passed；临时删除 fenced cleanup rejoin 保护，四个公开 dispose/aclose × 正常/失败用例均观察到 TimeoutError，恢复正确源码后通过。故反例确实进入清理主线，而非夹具启动失败。
- compileall、修改范围 Ruff、git diff --check 通过；collect-only 收集 4154 项，未执行全量。受影响文档相对链接全部存在，代码围栏闭合，两版 14.3.2 一一对应；秘密未进入文档。

上述集合有重叠，不把数字相加声称不同测试总数。离线模拟 Provider，不调用真实模型；使用既有明确 Docker 镜像完成定向 Product 固定验证，未拉取、安装或修复环境。没有运行全量、L2–L4、Wheel。

## 限制与下一阶段

工作路径是产物接受边界，不是 OS 沙箱；助手没有 Shell/网络/安装/递归/审批能力。收回 Patch 不等于整合，也不证明功能正确。WC-3 再接显式原文读取、前像冲突检查和可对账整合；人工 Promotion 仍独立。WC-4 的一次真实测试授权仍限定一题、32 次主子调用、600 秒、连接 60 秒、零重试，失败或封顶停止。

正式上下文同步 0/14.3/14.3.2、20.15、20.26–20.28 及协议引用；通俗版同步相同主题（历史 20.x 编号映射保持），入口/导航/计划同步当前进度。
