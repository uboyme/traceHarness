# TUI 同步与真实终端端到端验收

2026-09-13，用户明确要求同步 TUI 并进行端到端验收。保留已有工作区改动，不提交、推送或发版。

## 修复范围

上一轮新建 Product 表单保存失败已复现：默认验证命令遗漏协议 3 必填的 public_requirement。现已同时修复初始命令和新增命令；中文字段可填写说明，空白转为 null，长度、控制字符和协议合法性继续由原解析器判断。

roles.patch_author 默认关闭；选中后可以显式开启/关闭。开启生成独立草稿，复制当前调查助手预算，工具为 list_files/read_file/search_text/apply_patch。没有授予 Shell、递归团队或审批权限。主方和 multi 文案同步，旧 Router 空状态文案改为主方/助手。TaskConversation、Artifact 和 Approval 仍使用原 owner。

## 冻结与执行

独立 source/target.git、原 command-recovery 材料和固定检查，已有固定身份 Docker 镜像，网络关闭，不安装依赖或拉镜像。空实现 exit 1 且 AssertionError、参考实现 exit 0 的预检通过。

百炼 DeepSeek V4 Flash；一轮最多 40 次模型调用、720 秒、连接 60 秒、零 Provider 重试。任务累计 480000、主方子树 360000 tokens；权限与预算均在独立配置中显式提供，不作为产品默认。

配置阶段通过程序化 Textual 表单操作并调用原生产解析器保存。执行阶段是实际 Windows PTY 中运行的原 CLI `_chat` → 原 TUI runner → TracehTuiApp，Textual auto_pilot 负责输入和点击，**不是 headless 模型验收**。Provider 仅在测试驱动外包装整树调用上限；任务、工具、Store、Verifier、审批与收尾均未替换。

从聊天提议、后续确认、界面 START，运行到助手交回 Patch、主方显式整合、固定验证、Review；查看补丁和身份后输入 APPROVE。只审批隔离 target.git。批准前目标仍为初始版本，批准后才改变；source 保持干净，项目仓库没有提交或推广。

## 结果与证据

共 24 次调用：聊天 4、主方 15、助手 5；262.047 秒，193929 exact tokens（输入 180537、输出 13392）。Product completed，主方完成检查首次通过。助手补丁 1519 字节，最终补丁 2442 字节，只包含两份约定模块。

24 份冻结请求在账本副本重放通过；原不变量检查通过。3 个账户关闭、22 个预算预留结清、3 个工作区释放，6 份原 Sandbox reader/CAS 证据全部收敛。原数据库哈希为 `bf7b0d1784291de0b8a509d57130f1404e038bbe9cfc96eaa83dc13f500f28d5`，副本核验后未变化。

完成后通过 Ctrl+Q 正常触发原收尾，TUI 返回 130；它不代表任务被取消或 Promotion 失败。任务状态和目标 ref 是完成依据。

首张任务对话截图过早，只留下标题；随后用账本副本在真实 PTY 重开原任务对话页，等待实际内容加载，验证 coder/patch_author 两角色并展开助手，零模型调用。补丁和身份页原截图已有实际内容。驱动后续增加等待与角色断言，并对未完成结果返回失败退出码；没有重跑真实模型。原执行驱动按原摘要保存在本地验收目录的 driver.py。

本地证据目录为 `.traceh/tui-live-20260913`，包含配置、冻结合同、原执行驱动、原账本、结果、重放副本及 SVG 截图。可提交的非秘密统计见[摘要](../validation-data/dynamic-collaboration/tui-live/summary.json)。

## 定向回归与边界

表单回归覆盖缺失必填项、过长说明拒绝、说明清空、新增命令、助手启停、预算编辑互不影响，以及取消/外部修改保留原文件。旧真实沙箱 TUI 用例发现过期夹具：任务深度不足以容纳主方再建助手，且助手额度没有避开主方运行预留。仅修正测试预算与旧 auto/router/parent/reviewer 断言；原额度拒绝规则保持不变。修复前已观察到 budget-child-limits-invalid 和 BudgetExhaustedError，修复后实际进入协作及审批，非空路径验证。

最终 86 项定向/相邻回归全部通过（143.75 秒），无跳过；覆盖原真实沙箱审批、取消、SQLite 收敛与 single/multi 推广。compileall、4230 项仅收集、6 个修改 Python 文件 Ruff、文档链接/代码块/章节及 diff 检查通过。未跑全量、L2–L4、安装、发布门禁或第二轮真实模型。

通俗例子：现在能在界面明确勾选“允许助手写补丁”，把任务交给主方，看到实际补丁并最后批准落入测试仓库。一次走通证明这条入口接上了后台；不证明所有任务都稳定成功，也没有验证固定检查失败后消费新反馈再返工。任务对话仍是打开时快照，重新打开才刷新。
