# traceh-plugin-creator-skill-plugin

TraceHarness 的“插件创建技能”L1 实现。它本身是一个独立 Wheel，通过现有
`traceh.plugins` Entry Point 接入，只给模型一段简短 Prompt 和一个读取打包指南的
`PURE_READ` 工具。它不会生成第二套插件运行时，也不会自动安装或执行候选代码。

## 它能做什么

启用后，Agent 可以读取四份打包资源：

| Topic | 内容 |
|---|---|
| `workflow` | Candidate Workspace、安全边界和源码生成步骤 |
| `contract` | v0.5 公开 SDK、生命周期和禁止越界事项 |
| `template` | 独立 Distribution 的标准目录与结构模板 |
| `checklist` | 交付前的静态核对清单 |

指南要求 Agent 把源码直接写进一个独立 Candidate Workspace，并生成标为
`UNVALIDATED (L1 SOURCE ONLY)` 的 `CANDIDATE.md`。L1 不运行候选测试，也不构建、
安装、启用或提交候选；这些属于后续门禁。

L1 交付完成后，回到受信任的 TraceHarness 环境运行 L2；不要让候选自己执行或解释门禁：

```powershell
traceh plugins validate <candidate-workspace> `
  --core-project <trusted-traceh-git-repository> `
  --output <new-evidence-directory> `
  --allow-index
```

离线依赖改用 `--wheelhouse <directory>`。只有全部门禁通过才会在输出目录产生带
SHA-256 的候选 Wheel；失败报告不等于批准。虚拟环境也不是操作系统沙箱。

## 安装和体验

在 TraceHarness 仓库根目录安装此独立插件：

```powershell
python -m pip install .\examples\plugins\traceh-plugin-creator-skill-plugin
traceh plugins list
traceh plugins doctor traceh.plugin.creator
```

创建一个**不在 TraceHarness 仓库内**的空目录，再把它作为 Workspace：

```powershell
traceh chat <candidate-workspace> --plugin traceh.plugin.creator
```

然后明确提出要创建的插件能力。Agent 应先补齐并确认 plugin id、Distribution、
import package、entry class、version、贡献类型、权限和验收条件，再写文件。

若 Provider 配置位于其他目录，可显式使用 `--env-file <path>`；不要把真实 `.env`
复制进 Candidate Workspace。

## 边界

- 这是受信任的同进程插件，不是沙箱。
- 指南工具只读取 Wheel 内资源，不读取 Workspace、用户目录、环境变量或网络。
- Workspace 隔离和“不执行候选”是 L1 合同；本插件不会偷偷调用 pip、pytest 或
  `traceh plugins`。
- L2 验证由宿主命令负责；L3 比较、L4 人工批准/晋升和回滚仍未由本技能实现。
- Python Quality 插件只是已验证的真实插件参考，不会成为候选的隐藏默认能力。

完整插件作者合同见 [`../../../docs/plugins.md`](../../../docs/plugins.md)。
