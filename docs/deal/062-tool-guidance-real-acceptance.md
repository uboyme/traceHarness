# WC-4 工具说明修复后的真实验收

2026-09-13，用户另行授权一轮真实模型测试。使用当前配置的 deepseek-v4-flash，主子相同；冻结一题一次、主子合计最多 32 次调用、600 秒、连接 60 秒、零 Provider 重试。复用原 Evaluation/Product、已有断网 Sandbox 镜像、固定材料与验收标准。空实现 exit 1、参考实现 exit 0，随后只运行一次。

## 结果与改进

完整验收未通过，Product/Workflow failed，没有完成检查、正式 Review 或 Promotion。实际 25 次调用（主方 21、助手 4）、236855 Provider exact tokens、221985 ms，无 Provider 失败或重试。驱动退出零表示评估完成，不表示任务成功。

准备阶段成功批量读三份文件；分工首次把 main_work 写成字符串被拒，下一次改为对象后成功，只启动一个助手。助手交付 telemetry_rules.py 的 1146 字节 Patch；主方仅传 artifact_id，一次完整读取，next_offset 为 null，然后显式 integrate_child_patch 得到 applied。主方完成 telemetry_report.py。与记录 060 不同，本轮没有调用未提供的工具，也没有反复小页阅读。

## 实际失败链

主方之后 12 次 shell 调用中，8 次使用以 `cd /workspace &&` 开头的命令，均 start-failed、exit_code null；4 次直接执行 python3 成功，分别为版本、打印、导入和 normalize 断言测试。当前工具名称虽然是 shell，公开合同和系统提示明确它按 argv 执行、不启动系统 shell；因此 `cd` 被当成待执行程序，不是 shell 内建命令。不是 Python 不可用或 Provider 连接失败。失败回执只给通用 start-failed 与空 stderr，诊断提示不足，模型又多次重复相同用法；这属于工具使用及失败反馈层的后续修复方向，不宜靠扩权或加预算掩盖。

最后一条 normalize 断言命令确实 exit 0，但 summarize 尚未测试。下一步在原 Budget admission 以 max_tokens 不足停止，未到 32 次调用上限或总超时；本轮没有完成声明被退回，也不能套用旧轮“忽略完成反馈”的原因。两模块代码已写出不等于固定功能验收通过，不把静态核读或局部自测补算为成功。

通俗例子：这次助手的作业已经交来，主方也完整看完并合入了；随后主方拿“终端语法”去操作一个“直接启动程序”的按钮，反复碰壁，等终于跑通部分自测时预算已经不足以收尾。

## 证据与边界

原交接正文进入后续 17 份主方请求；旧 reports_dispatched_to_parent 仍报 0，属于已有观测口径缺口。3 个预算账户关闭、27 个使用预留结清；账本结算 232483 tokens，24 笔 exact、1 笔 unknown、2 笔无 usage，不能用结算数代替 Provider 报告。工作区 live 0、released 1、quarantined 2；12 份 Sandbox 证据经原 reader/CAS 校验且全部 converged。27 份请求在数据库副本上重放和不变量检查通过，原数据库未变。

- 冻结合同 SHA-256：`0bdd7e16473873dce89332e03b611f8432a5acf3f75f7fc1d66836af1d277837`。
- 原数据库 SHA-256：`3471d26a841adc3557ab256f77e156edad812ffcad8207ead5c7c4c62ccf271c`。
- 可分享的脱敏统计见[结果摘要](../validation-data/dynamic-collaboration/writable-guidance/summary.json)。原始材料、轨迹及只读副本保留在本地该轮独立数据目录。

本轮仅新增验收记录、摘要并同步两份上下文的 1、14.3.10–11、15、16 及入口状态；没有修改生产源码、评分或权限。原记录 061 的 210 项定向测试是既有实现证据，本轮不冒称重新运行。冻结源码/材料复核、文档章节/链接/代码块检查及 diff 检查通过；前五轮原库摘要不变。未跑全量、L2–L4、收益对照、追加模型调用、提交、推送或发布。WC-4 仍未完整通过。
