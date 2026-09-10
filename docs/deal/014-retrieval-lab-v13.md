# 014：用户工作区检索体验核对与逐行手册

## 范围与发现

2026-09-10 核对用户本机 `tracehtest` 夹具，不改产品检索、记忆、压缩或沙箱实现。
日常启动配置只装配沙箱，未选 Context/Skill；旧 lab profile 指向另一数据目录和固定旧 Session。
只读检查确认旧两处会话分别为协议 9、10，新沙箱会话为 13；当前解析器要求 Session 13 / Context 12。
旧手册仍描述 Session 10。原测试 Skill 插件在夹具专用 `.packages`，不是全局安装项。

## 准备内容

在用户夹具目录新增独立检索 profile、v13 逐行手册和结构预检脚本。profile 选用已有 Skill/Context，
工作区限定为 `workspace`，预定新 `.retrieval-v13` 数据目录，不带固定 Session，不装配沙箱或 Product，
关闭自动压缩用于可控的手动 History 实验。没有读取密钥或修改现有启动配置、旧数据库和记忆。

原夹具 `lab.py` 增加显式 `--profile`；启动、会话列表与压缩共同读取指定 profile，仍委托原 CLI 和
Session/Compaction owner。旧默认 profile 不改；压缩读取前检查当前协议，不解释旧 Session。
这只是用户本机的显式测试夹具，不是发布包的新命令或隐藏默认。

手册把自然问句与失败后的有提示诊断分开，区分答案正确、搜索命中与原文读取；Memory 审批使用原 F4
表单，不要求手抄提议 UUID。先做当前聊天与跨 Session 隔离，再压缩原会话验证自然找回，避免用已经
回到最近对话的答案冒充历史召回。不把每题都调用搜索作为成功条件。

## 验证与边界

当前配置解析及插件发现通过；原治理、TUI 治理与 Skill/Memory/History 主动搜索 5 个文件共
68 项定向检查通过（103.549 秒，0 skipped）。本地预检用脚本 Provider 驱动真实插件、Runtime、
SQLite、搜索／读取、项目批准和原请求重放，数据库全在临时目录。
初版预检自身重复使用 ToolCall ID、把未绑定 Memory 读取当空列表；已按原合同修正夹具，不放宽核心协议。

实际报告随用户本机手册保存在 `tracehtest/retrieval-preflight-v13.json`；预检不能证明真实模型会自主
正确检索，手册的自然语言交互仍需用户实际体验。未运行全量、L2 或真实模型 API，没有迁移、删除、提交或发布。
