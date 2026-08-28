# traceh-python-quality-plugin

从早期版本延续至 TraceHarness v0.7 的独立 Python Quality 插件。它兼容 `traceharness-py>=0.5,<0.8`，不是核心包里的隐藏默认能力，安装后仍需显式启用。

## 能力

| 类型 | 名称 | 行为 |
|---|---|---|
| Tool | `python_project_info` | 只读工作区根目录的固定 Python 项目元数据，不执行项目代码 |
| Prompt | `traceh.python.quality` | 要求先检查项目，并以 Verifier 结果作为完成证据 |
| Policy | `python-environment-safety` | 拒绝 `pip uninstall` 以及逃离当前环境的安装目标 |
| Verifier | `python-tests` | 运行项目明确声明的测试命令；必须由操作员显式选择 |

插件不读取用户目录、环境变量或网络。它只读取工作区根目录下的 `pyproject.toml`、`pytest.ini`，并检查 `src/`、`tests/` 是否存在；解析前会验证路径仍位于 Workspace 内。

## 安装与启用

在仓库根目录：

```powershell
python -m pip install .\examples\plugins\traceh-python-quality-plugin
traceh plugins list
traceh plugins doctor traceh.python.quality
```

给项目的 `pyproject.toml` 明确配置测试命令：

```toml
[tool.traceh-python-quality]
test-command = ["python", "-m", "pytest", "-q"]
timeout-seconds = 120
```

然后显式启用插件和命名 Verifier：

```powershell
traceh chat <workspace> `
  --plugin traceh.python.quality `
  --plugin-verifier python-tests
```

如果项目已经通过 `pyproject.toml` 的 `[tool.pytest.ini_options]` 或根目录 `pytest.ini` 明确声明 pytest，插件会使用当前 TraceHarness Python 解释器运行 `python -m pytest -q`。除此之外它不会猜测测试框架；缺少证据时 Verifier 会明确失败并告诉你补配置。

## 安全边界

- 插件与 TraceHarness 同进程、同权限；启用它仍等于信任它的代码。
- Policy 是单调 Deny Guardrail，不是沙箱。
- 明确配置的测试命令由项目作者负责；执行仍走 TraceHarness 的现有 `CommandVerifier` 子进程收敛语义。
- 安装只让插件可发现，不会自动启用，也不会在运行中替换 Wheel。
