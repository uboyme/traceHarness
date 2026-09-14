# 0072：独立可写助手与主工作区内的显式整合

2026-09-13，WC-2 编码前冻结。状态：已接受设计，尚未实现；实施结果以正式上下文及阶段记录为准。

## 授权与装配

沿用 single/multi、原 Workflow 与 Supervisor。Profile 增加显式 `patch_author` 槽位（角色配置或 null）；null 表示未授权写助手。非 null 时 multi 的唯一助手使用该槽位，single 不派发助手。investigator 仍为只读调查能力，原调查工具不扩权。可写槽位只允许 list_files/read_file/search_text/apply_patch，必须包含 apply_patch，且 child/depth 为零；没有 shell、网络、安装、递归、审批或推广能力。

Product protocol/host config 切换到 6，Product event schema 切换到 5；显式拒绝旧合同，不迁移、删除或猜测旧配置。Session 14、Context 12、SQLite 2、Inbox/Delivery、Workspace、Artifact 和 Promotion 格式保持。readonly work 2 保持；新的 `writable-assignment` format 1 单独定义，不在调查信封中加入 writable 布尔值。

## 工作与交接

work 绑定 task、direct parent、预先派生的 child/session/message、source id、精确 revision、预算及实际装配摘要、主方工作与助手目标/范围/排除项/交付/简报，以及非空相对文件路径清单。身份由宿主从原 Product/Directory/Workspace/Budget 装配得出，模型只提供工作与路径意图。路径清单在接收 Manifest 时核对；它不是文件级操作系统沙箱。

从同一冻结 revision 创建独立工作区，不复制主方脏修改。原 Supervisor create/send/wait/report 运行一次消息；完成后原 CaptureGate/PatchCaptureService 捕获，不信任模型自报 Artifact ID。重复 collect 读取同一已捕获身份及 CAS，不再次执行或捕获。完成交付后不提供 followup，捕获与原 send/resume/close 共用原 gate。失败或取消先收敛 child，资源由原 Product owner 按已有规则释放；脏失败证据 quarantine，不擅自丢弃。成功与人工拒绝沿已有 release_captured 规则清理，捕获先于释放。

## WC-3 操作合同（此刻只冻结，不提前实现）

主方先调用只读 Artifact 阅读工具取得源 Patch 的有界页；原 Session/Effect 记录页及 digest。整合必须证明同一主方已读取完整 Patch。模型显式选择已交接 artifact_id；宿主重新核对 direct ownership、task/source/base、完成消息、Manifest/CAS、目标 Workspace 与 caller Session。

WC-3 实施细化：read_child_patch 通过原 Artifact/CAS 读原文页，并返回目标 generation 与文件前/后像组成的 request digest；integrate_child_patch 显式引用该读回执及摘要。原 read Effect 保存完整冻结请求，原 write Effect 的 arguments 引用它，不新增准备事件。原文可见性需核对实际冻结模型请求，分页覆盖不足拒绝。对象物化只读取同一来源 Git 的 base/candidate tree，重新生成 Patch bytes 与 CAS 精确比对后读取 blob；对象不可用明确失败，不联网补取或猜测内容。

Workspace 受控编辑接受冻结文件前/后像，复用原 WorkspaceService 的身份解析与修改锁。逐文件原子写入，文件 mode 与实际字节摘要均进入回执；本机不支持的 mode 变化明确拒绝。ToolRuntime 使用通用 typed failure/cancellation 输出在原 effect/outcome 留存回执，仍由原 Result/Recovery 补齐，不另写整合账本。整合后即使已取消，也先完成回滚或明确对账状态及 Outcome 再返回。

整合工具为 WORKSPACE_WRITE，执行由 Workspace 受控编辑服务拥有。operation_id 从 caller Agent/Session/Turn/Step/tool_call_id 派生；请求摘要包含 artifact_id、Manifest digest、Patch digest、target workspace id/generation、source/base，以及有序文件前/后像（存在性、blob/bytes digest、mode）。同一 operation 不同请求拒绝。原 Effect intent/dispatched 证明一次性派发，回执保留在该 Effect outcome；不增加 applied_patches 表、并行状态机或新事实流。

先验证所有文件前像精确匹配源版本，再逐文件原子替换/创建/删除。主方已经修改目标文件或目标漂移即冲突；不自动三方合并，不覆盖主方修改。多文件发布不声称全目录事务：工作归 owned task，失败时按前/后像对账，只回滚仍等于本次后像的文件，保留不明或第三方改动，回执逐项区分 applied/rolled_back/unchanged/conflict/unknown。取消先等操作、回滚与持久回执收敛，再传播原取消。

重复或结果不明时只从原 Effect、Artifact 与实际文件核对，不自动再发布。已应用后主方再改文件时，不用旧回执覆盖新内容。新 tool_call_id 不能绕过前像冲突重套同一 Patch。任何不能证明的状态明确失败并保留证据。

内部整合不调用 Promotion、不更新外部 ref、不生成 Approval。最终汇总 Patch 仍经原固定 Verifier、人工批准及 Promotion；隔离 benchmark 的冻结批准策略仅作用于其一次性目标。

## 验证与停止

WC-2 的重复取消公开路径揭示原关闭重入死锁：祖先 scope 等待主方退出，主方的助手 stop 又排在同一 scope 后。修复由原 Supervisor/Lifecycle 拥有：仅在 admission 已封闭并静止、对应 child-first cleanup Task 已存在时加入该同一 Task，包括其错误；不重新进入 disposal scope，不跨恢复代际复用。Product 不获得 lifecycle 内部状态，也不另建清理器。正常/失败及 dispose/aclose 四组反向验证均能在移除保护时复现超时，恢复后通过；原源码保护摘要随该通用修复更新，其他受保护内核不变。

WC-2 先验身份/权限、独立版本、完成/失败/取消、重复读取、捕获与清理；WC-3 再验整合正向、冲突、部分失败、重复及取消收敛。使用公开 Product/Tool/Workspace 路径，关键保护做反向验证。只跑定向及相邻回归、compileall、collect-only、Ruff 与文档检查，不跑全量或 L2–L4。

WC-4 仅一冻结业务题；主子总计最多 32 次、600 秒、单连接 60 秒、零重试。空交付失败/参考通过预检后执行，Provider 失败、取消、封顶或环境未就绪停止；不调提示追跑、不添加收益对照。未执行的门禁不宣称通过。
