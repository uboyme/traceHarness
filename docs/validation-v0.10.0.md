# v0.10.0 限定发行验证

本次用户授权提交、发布 GitHub Release，明确不跑全量和 L2。以下是限定检查，不声称通过完整发布门禁。

已有工程证据：

- [S0–S4](validation-sandbox-s0-s4.md)：463 项合并定向检查，随后 4 项观察回执补查和 1 项取消补查；含真实 Docker 执行、取消、限额、文件发布、插件生命周期和失败路径。
- [环境选择](deal/013-sandbox-environment-picker.md)：133 项定向检查，真实 Docker 下拉/手填解析与 Textual 保存，以及移除保护后的反向验证。
- [检索体验核对](deal/014-retrieval-lab-v13.md)：68 项相邻回归；临时数据中的脚本化协议检查不等于真实模型效果复测。

上述集合存在重叠，不能相加为独立测试总数。没有调用真实模型服务。

两份项目上下文同步第 1 节、版本概况、第 7.11 节和第 15 节；第 16 节保留现有支持范围和风险。

## 本次发行包检查

- 12 个相关测试文件最终 **192 passed，0 skipped**，耗时 121.575 秒；直接导入隔离安装的 0.10.0 Wheel，包含真实 Docker shell/Verifier、权限与资源限额、宿主退出、插件和配置界面。
- `compileall src tests`、83 个修改范围 Python 文件 Ruff、`git diff --check` 通过；collect-only 收集 3744 项，未执行全量。
- 从新建干净目录离线构建 Wheel，285 个包内文件集合与内容逐字节等于源码；使用 `--no-index --no-deps --target` 安装到独立目录，未重装正在使用的 TUI。依赖复用当前解释器，不声称全新 core-only 环境。
- `python -I` 验证隔离包导入、0.10.0 metadata、CLI help、scripted doctor 与 Linux guest 资源文件；Linux guest 的实际执行由上述真实 Docker 用例验证。
- 两份上下文 0–20 章节对应；修改文档的相对链接和 Mermaid 围栏检查通过。11 个沙箱核心文件与 S0–S4 最终证据哈希一致。
- 源码 ZIP 从审阅后的 Git 文件集合生成，并由打包脚本逐字节回读；发行资产另附 SHA256SUMS。

首轮检查没有隐去：开发路径先读到旧 0.9.0 安装元数据；调整为隔离安装包后版本合同 10 项通过。另有一次 Textual 树节点选择断言失败（尚未发起 Docker 查询），同代码独立复跑和整组最终复跑通过；尚未证明该偶发同步问题的根因，也不声称已修复。临时打包探针曾错误按 LF 匹配 Windows metadata、在宿主直接 import Linux guest，这两项探针已按 metadata 行解析和容器资源边界修正，未放宽产品协议。

机器可读结果见 [checks.json](validation-data/release-v0.10.0/checks.json)。既有 463/133/68 与本次 192 项有重叠，不能累计为独立测试总量。
