# AO-3 限定真实验收

原运行：2026-09-11，qwen-plus 直连，显式 opt-in driver `tests/live_optimization/background.py`。一轮 Skill Chat 后提交未标注反馈，再跑原受限提案、s-direct/s-absent 两题 seed 113 的双臂和四次语义裁判。样本身份仅属于本次测试。

10 个 Session、21 次请求、126,191 exact tokens、0 失败；原账本重放和不变量通过，757 个原文件未改变。任务模型评分 1/2→2/2，Token 27,820→34,498，工具 3→4，超过预先冻结的 1.15 倍费用门槛，因此未晋级、未采用。前台 26,062、分析 5,885、任务 62,318、裁判 31,926 tokens。

[原始归档](original-evidence.zip) 保留请求、Session/Effect、冻结源码和原定位，不能手改路径或评分后声称仍是原证据。运行时源码版本标识为 0.10.0，包含尚未发行的 AO-3；最终 0.11.0 另有立即退出修复及 UI 向导，定向和反向检查记录在 [reverse.json](reverse.json) 与发行验证。没有把旧实验改写成最终版全覆盖证据。

小样使用模型裁判，不能证明泛化提升，也不替换旧 72 题结果。原环境绝对定位失效时，需要使用原冻结源码及明确 relocation 验证，不能静默改报告。发行与定向结果见 [v0.11.0](../../../validation-v0.11.0.md)。

## 最终安装包补测

最终 0.11.0 wheel 在独立 Windows venv 又运行同一规模的小样：10 个 Session、21 次请求、125,128 exact tokens、0 失败，759 个原文件不变且请求重放通过。任务模型评分仍为 1/2→2/2，任务 Token 34,376→28,090、工具 4→3，本轮达到原冻结门槛并进入 review_candidate，未自动采用。前台 24,729、分析 5,942、任务 62,466、裁判 31,991 tokens。这是另一轮独立小样；前一轮成本超标记录仍保留，不拼分、不声称普遍改善。

见 [原始证据](release-install-evidence.zip) 与 [独立重开摘要](release-install-summary.json)。回执 format 2 同时核对宿主/启动器与实际 worker 的进程所有权链。

## 定向与发行检查

发行定向首批 318 passed / 12 skipped，在一个过期的 0.10 版本重复断言处停止；删除两处重复版本断言、由原 test_version_contract 唯一检查 0.11 版本后，架构/版本/TUI 及相邻合同批次为 110 passed / 18 skipped。未修改已经通过的执行源码，未把两批相加；跳过项是未显式配置的 Docker 测试。另显式使用现有测试镜像，Product 两臂经过真实 Git、Docker 验证与原推广链，1 passed。编译、修改范围 Ruff、collect-only 3,978 条、diff-check 和文档检查通过；五项关键保护的反向检查均因预期根因失败。新 wheel 离线安装后 316 个包文件与源码逐字节一致，CLI/doctor 正常。没有运行全量 pytest 或 L2–L4，也没有把模型语义判断当成人工评分。

原首批日志保留版本断言失败，不隐藏失败；修正后的后续检查另见 targeted-tail.log。installed-reverse.json 记录在原包副本中恢复故障逻辑的两项检查，安装包原件未被修改。
