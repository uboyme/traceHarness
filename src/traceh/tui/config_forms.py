"""Chinese structured editors for the existing host configuration documents."""

from __future__ import annotations

import asyncio
import unicodedata
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Select, Static, Tree

from traceh.chat.config import parse_context_host_config
from traceh.cli.tui_config import LaunchConfigurationError, atomic_json
from traceh.concurrency import await_worker_convergence
from traceh.product.config import parse_product_host_config
from traceh.promotion.models import PROMOTION_PROTOCOL_VERSION
from traceh.sandbox.config import SANDBOX_CONFIG_FORMAT, parse_sandbox_config
from traceh.tui import docker_choices

# Labels are presentation metadata, not another schema or runtime policy parser.
LABELS = dict(
    line.split("=", 1)
    for line in """
context=知识检索与预算
skill_policy=Skill 文件来源与大小限制
project=项目与记忆来源
skills=Skill 检索
memory=项目记忆检索
history=压缩历史原文读取
history_tier=历史初次披露方式
total_bytes=补充上下文总上限（字节）
history_bytes=历史内容上限（字节）
item_bytes=单条资料上限（字节）
max_blocks=最多资料块数
max_exclusions=最多排除原因数
max_query_bytes=检索问题最大长度（字节）
workspace_observations=检查工作区版本是否变化
local_lanes=本地额外检索通道（当前不支持启用）
semantic=向量检索（当前不可用）
reranker=重排模型（当前不可用）
unicode_version=文字规范化版本
default_tier=首次披露方式
match_fields=精确匹配字段
k1=词频影响系数
b=文档长度修正系数（0～1）
rrf_constant=多路排名融合常数
exact_weight=精确匹配权重
fts_weight=文本检索权重
context_bytes=本类资料预算（字节）
max_catalog_bytes=目录大小上限（字节）
max_terms=查询拆分词项上限
max_corpus_items=索引条目上限
max_corpus_bytes=索引内容上限（字节）
max_candidates=最多召回候选数
max_requests=每轮最多读取次数
limits=数量与大小限制
max_skills=最多 Skill 数量
max_summary_bytes=简介大小上限（字节）
max_content_bytes=正文大小上限（字节）
max_resource_bytes=资源大小上限（字节）
resource_roots=Skill 资源目录列表
plugin=已安装插件身份
plugin_id=插件 ID（与安装信息一致）
version=插件版本（与安装信息一致）
path=资源目录路径
sources=来源名称与 Git 工作区目录
managed_root=受管工作区存放目录
max_catalog_events=项目目录事件上限
max_label_bytes=项目名称长度上限（字节）
memory_policy=记忆内容与来源限制
max_body_bytes=单条记忆正文上限（字节）
max_sources=单条提议来源数量上限
max_source_events=读取来源事件上限
max_source_bytes=读取来源大小上限（字节）
max_memory_events=项目记忆事件上限
denied_patterns=拒绝写入记忆的内容规则（正则表达式）
max_depth=最大嵌套深度
page_bytes=每页原文上限（字节）
page_messages=每页最多消息数
protocol_version=协议版本（由系统确定）
format=文件格式版本（由系统确定）
profile_id=任务配置标识
approver_id=审批人署名（请填写）
provider_id=模型接入方式
model_id=模型名称
default_mode=任务执行模式
source=要处理的 Git 项目
source_id=来源标识（与记忆来源一致）
repository=仓库绝对路径
revision=起始 Git 版本或分支
promotion_target=结果接收仓库（当前要求本地 bare Git 仓库）
target_id=接收目标标识
ref=接收分支完整名称（请填写 refs/heads/…）
managed_workspace_root=隔离工作区存放目录
cas_root=补丁原文存放目录
roles=各角色的权限与预算
parent=协调角色
reviewer=审查角色
coder=编码角色
preset=内置角色预设
capability_grants=允许使用的工具
max_output_tokens=单次回复 token 上限
budget=该角色预算
max_tokens=累计 token 上限（空白表示不限制）
max_steps=最多推理步骤（空白表示不限制）
max_tool_calls=最多工具调用（空白表示不限制）
max_wall_milliseconds=最长运行时间（毫秒，空白表示不限制）
max_children=最多子任务（空白表示不限制）
max_processes=最多进程（空白表示不限制）
router=自动选择任务模式
timeout_milliseconds=选择模式超时（毫秒）
max_response_bytes=模式选择回复上限（字节）
task_budget=整个任务的预算
verification=完成后的验证方案
plan_id=验证方案标识
plan_version=验证方案版本
commands=验证命令列表（不会在配置时执行）
command_id=命令标识
argv=命令及参数（每项一个参数，不经过 Shell）
timeout_ms=命令超时（毫秒）
environment=验证命令环境
policy_id=环境策略标识
passthrough=允许传入的环境变量名称
overrides=固定环境值（不能填真实密钥）
max_output_bytes=验证输出上限（字节）
capture_limits=补丁采集大小限制
max_changed_paths=最多改动文件数
max_path_bytes=文件路径上限（字节）
max_file_bytes=单个文件上限（字节）
max_total_file_bytes=文件合计上限（字节）
max_patch_bytes=补丁上限（字节）
max_report_chars=界面报告上限（字符）
policy=沙箱主机授权策略
docker_context=Docker 连接（下拉选择或手动填写名称）
image=运行环境（下拉选择或手动填写镜像名称／ID）
network=网络访问（当前只支持关闭）
read_paths=允许读入沙箱的相对目录／文件（添加 . 表示整个工作区）
write_paths=允许回写的相对目录／文件（必须在读入范围内）
excluded_paths=额外排除的相对目录／文件（不会读入或回写）
memory_bytes=容器内存上限（字节，至少 67108864）
workspace_bytes=工作区文件合计上限（字节）
workspace_files=工作区节点数量上限（包括目录）
output_bytes=每路标准输出／错误上限（字节，超出终止）
pids=容器进程与线程合计上限（至少 4）
cpus=CPU 配额（1 表示一个逻辑核）
wall_seconds=单次命令最长秒数（工具／验证只能进一步缩小）
plugin_grants=允许启动外部进程的插件（空列表表示不授权）
stdio=标准输入输出连接额度
input_bytes=这次进程的输入总上限（字节）
frame_bytes=每次收发上限（字节，不大于输入总上限）
workspace=插件服务器读取的工作区绝对路径
""".strip().splitlines()
)

CHOICES = {
    "network": [("关闭网络访问", "none")],
    "default_mode": [
        ("自动选择", "auto"),
        ("单个编码角色", "single"),
        ("协调＋审查＋编码", "multi"),
    ],
    "default_tier": [
        ("目录：只告知资料存在", "directory"),
        ("简介：Memory 会给出完整事实", "summary"),
    ],
    "history_tier": [("目录：需要时读取原文", "directory"), ("压缩摘要", "summary")],
}


def retrieval_preset():
    return dict(
        unicode_version=unicodedata.unidata_version,
        default_tier="directory",
        match_fields=["id", "tag", "path", "symbol", "error"],
        k1=1.2,
        b=0.75,
        rrf_constant=60,
        exact_weight=2,
        fts_weight=1,
        context_bytes=24000,
        max_catalog_bytes=32000,
        max_terms=512,
        max_corpus_items=100,
        max_corpus_bytes=100000,
        max_candidates=20,
        max_requests=8,
    )


def context_preset():
    """Visible editable starter values; no project or plugin is implicitly selected."""
    return {
        "format": 1,
        "context": dict(
            total_bytes=64000,
            history_bytes=0,
            item_bytes=32000,
            max_blocks=20,
            max_exclusions=40,
            max_query_bytes=4000,
            history=None,
            history_tier=None,
            skills=None,
            memory=None,
            local_lanes={"semantic": None, "reranker": None},
            workspace_observations=False,
        ),
        "skill_policy": None,
        "project": None,
    }


def product_preset(workspace: str, data_dir: str, provider: str, model: str):
    base = Path(data_dir or ".traceh").expanduser().resolve()
    budget = dict(
        max_tokens=120000,
        max_steps=40,
        max_tool_calls=80,
        max_wall_milliseconds=600000,
        max_children=4,
        max_depth=2,
        max_processes=4,
    )
    roles = {}
    for name in ("parent", "reviewer", "coder"):
        grants = ["list_files", "read_file", "search_text"]
        if name == "coder":
            grants += ["apply_patch", "shell"]
        roles[name] = dict(
            preset="coding-role",
            capability_grants=grants,
            max_output_tokens=4096,
            budget=deepcopy(budget),
        )

    def identity():
        return str(uuid4())

    return dict(
        protocol_version=1,
        profile_id=identity(),
        approver_id="",
        provider_id=provider,
        model_id=model,
        default_mode="auto",
        source=dict(source_id=identity(), repository=workspace, revision="HEAD"),
        promotion_target=dict(target_id=identity(), repository="", ref=""),
        managed_workspace_root=str(base / "managed"),
        cas_root=str(base / "artifacts"),
        roles=roles,
        router=dict(
            preset="mode-router",
            max_output_tokens=512,
            budget=deepcopy(budget),
            timeout_milliseconds=30000,
            max_response_bytes=4096,
        ),
        task_budget=budget,
        verification=dict(
            plan_id=identity(),
            plan_version=1,
            commands=[dict(command_id=identity(), argv=[], timeout_ms=60000)],
            environment=dict(policy_id=identity(), passthrough=[], overrides={}),
            max_output_bytes=1048576,
            protocol_version=PROMOTION_PROTOCOL_VERSION,
        ),
        capture_limits=dict(
            max_changed_paths=100,
            max_path_bytes=1024,
            max_file_bytes=1048576,
            max_total_file_bytes=4194304,
            max_patch_bytes=4194304,
        ),
        max_report_chars=4096,
    )


def sandbox_preset():
    """Visible editable draft; backend identity and path grants require user input."""
    return dict(format=SANDBOX_CONFIG_FORMAT, plugin_grants=[], policy=dict(
        docker_context="", image="", network="none",
        read_paths=[], write_paths=[], excluded_paths=[],
        limits=dict(memory_bytes=268435456, workspace_bytes=33554432,
                    workspace_files=2048, output_bytes=1048576, pids=64,
                    cpus=1.0, wall_seconds=60.0),
    ))


def validate_document(kind, raw, path):
    if kind == "context":
        return parse_context_host_config(raw, path=path)
    if kind == "sandbox":
        return parse_sandbox_config(raw)
    if kind != "product":
        raise ValueError("unknown-configuration-kind")
    return parse_product_host_config(raw)


class ConfigForm(Screen[Path | None]):
    """Edit one draft and save only after the original parser has accepted it."""

    BINDINGS = [Binding("escape", "back", "取消", priority=True)]
    CSS = """
    ConfigForm { layout: vertical; }
    #config-form-title { height: auto; padding: 1; }
    #config-form-body { height: 1fr; }
    #config-tree { width: 1fr; }
    #config-fields { width: 1fr; padding: 0 1; }
    #config-help { height: auto; margin-bottom: 1; }
    #config-form-status { height: auto; max-height: 4; color: $warning; }
    #config-form-actions { height: 3; }
    """

    def __init__(self, kind, path, raw, *, expected_bytes=None, workspace="", data_dir=""):
        super().__init__()
        self.kind, self.path = kind, Path(path).expanduser().resolve()
        self.raw = deepcopy(raw)
        self.expected_bytes = expected_bytes
        self.workspace, self.data_dir = workspace, data_dir
        self.selected_path = ()
        self._docker_options: dict[str, list[tuple[str, str]]] = {}
        self._docker_task: asyncio.Task | None = None

    def compose(self):
        yield Static(
            {"context": "知识与记忆配置", "product": "任务执行配置",
             "sandbox": "执行沙箱配置"}[self.kind]
            + " · 左边选项目，右边查看说明并修改。预设值都可检查；不会执行命令或审批。",
            id="config-form-title",
            markup=False,
        )
        with Horizontal(id="config-form-body"):
            yield Tree("配置项目", id="config-tree")
            with VerticalScroll(id="config-fields"):
                yield Static("选择左侧项目。", id="config-help", markup=False)
                yield Select([], prompt="刷新后选择；也可在下方手动填写", id="docker-options")
                yield Button("刷新可选项", id="docker-refresh")
                yield Input(id="config-value")
                yield Select([], id="config-choice")
                yield Button("更新这个值", id="config-update")
                yield Button("开启／关闭这项功能", id="config-toggle")
                yield Label("新增来源名称／环境变量名（仅表格新增时填写）")
                yield Input(id="config-new-key")
                yield Button("添加一项", id="config-add")
                yield Button("删除所选列表项／来源", id="config-remove")
        yield Static("保存位置：" + str(self.path), id="config-form-status", markup=False)
        with Horizontal(id="config-form-actions"):
            yield Button("校验并保存使用", id="config-save", variant="primary")
            yield Button("取消", id="config-cancel")

    def on_mount(self):
        self.rebuild()

    def value(self, path):
        result = self.raw
        for key in path:
            result = result[key]
        return result

    def assign(self, path, value):
        self.value(path[:-1])[path[-1]] = value

    def rebuild(self):
        tree = self.query_one(Tree)
        tree.clear()
        tree.root.data = ()

        def add(parent, value, path):
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in entries:
                label = LABELS.get(key, str(key)) if isinstance(key, str) else f"第 {key + 1} 项"
                if item is None:
                    label += "（未设置／关闭）"
                elif not isinstance(item, (dict, list)):
                    label += "：" + (
                        "开启" if item is True else "关闭" if item is False else str(item)
                    )
                node = parent.add(Text(label), data=(*path, key), expand=len(path) == 0)
                if isinstance(item, (dict, list)):
                    add(node, item, (*path, key))
                if node.data == self.selected_path:
                    ancestor = node.parent
                    while ancestor is not None:
                        ancestor.expand()
                        ancestor = ancestor.parent
                    tree.select_node(node)

        add(tree.root, self.raw, ())
        tree.root.expand()
        self.show_field()

    @on(Tree.NodeSelected)
    def selected(self, event):
        self.selected_path = event.node.data or ()
        self.show_field()

    def show_field(self):
        path = self.selected_path
        value = self.value(path)
        key = path[-1] if path else ""
        scalar = not isinstance(value, (dict, list))
        title = " → ".join(LABELS.get(k, str(k)) for k in path)
        if self.kind == "sandbox" and key == "max_processes":
            title = "这次插件激活最多尝试启动几次（失败也计数，不能留空）"
        hint = "填写该字段的值，然后点更新。路径按保存文件所在目录解析；任务仓库须填绝对路径。"
        if not path:
            hint = "先展开左侧分组，选择要修改的项目。知识功能在「知识检索与预算」中开启。"
        if isinstance(value, list):
            hint = "展开查看各项；点击添加，再选新项填写。命令参数每项一个，无需 JSON 或额外引号。"
        elif isinstance(value, dict) and path:
            hint = "展开查看各项。来源和环境值可以添加命名项；其他分组字段由原配置合同确定。"
        elif (isinstance(key, str) and key.startswith("max_")) or isinstance(value, (int, float)):
            hint = "填写数字，单位见标题。角色预算可留空表示不限制；其他限制必须符合原配置规则。"
        if key in {"repository", "revision", "ref", "approver_id"}:
            hint = "此项必须明确填写。不会自动创建仓库、选择目标分支或代替你审批。"
        docker_field = self.kind == "sandbox" and path in {
            ("policy", "docker_context"), ("policy", "image"),
        }
        picker = self.query_one("#docker-options", Select)
        picker.display = docker_field
        self.query_one("#docker-refresh").display = docker_field
        if docker_field:
            hint = (
                "点击刷新后下拉选择，或直接在输入框填写，再点更新。切换连接会清空旧镜像。"
                if key == "docker_context" else
                "先更新 Docker 连接，再刷新镜像列表。可手填名称:标签或完整 ID，"
                "点更新后解析为固定身份；保存时再核对。不会下载或运行镜像，"
                "镜像中的 Python 和项目依赖仍需在实际执行时验证。"
            )
            with self.prevent(Select.Changed):
                available = self._docker_options.get(key, [])
                picker.set_options([(Text(label), identity) for label, identity in available])
                if value in {identity for _, identity in available}:
                    picker.value = value
                else:
                    picker.clear()
        self.query_one("#config-help", Static).update(title + "\n" + hint)
        self.query_one("#config-value", Input).value = "" if value is None else str(value)
        options = CHOICES.get(key, [("开启", True), ("关闭", False)] if type(value) is bool else [])
        choice = self.query_one("#config-choice", Select)
        choice.set_options(options)
        if options and value is not None:
            choice.value = value
        else:
            choice.clear()
        choice.display = bool(options)
        self.query_one("#config-value").display = scalar and not options
        self.query_one("#config-update", Button).disabled = (
            not path
            or not scalar
            or key in {"format", "protocol_version", "unicode_version", "semantic", "reranker"}
            or path in {("context", "history"), ("context", "skills"), ("context", "memory")}
        )
        self.query_one("#config-toggle", Button).display = path in {
            ("context", "history"),
            ("context", "skills"),
            ("context", "memory"),
        }
        dynamic = key in {"sources", "overrides"}
        self.query_one("#config-add", Button).disabled = not isinstance(value, list) and not dynamic
        self.query_one("#config-new-key").display = dynamic
        removable = bool(path) and (
            isinstance(self.value(path[:-1]), list)
            or (len(path) > 1 and path[-2] in {"sources", "overrides"})
        )
        self.query_one("#config-remove", Button).disabled = not removable

    @on(Select.Changed, "#docker-options")
    def docker_selected(self, event):
        event.stop()
        if not event.select.is_blank():
            self.query_one("#config-value", Input).value = str(event.value)

    def _start_docker(self, action):
        if self._docker_task is not None and not self._docker_task.done():
            return
        path = self.selected_path
        text = self.query_one("#config-value", Input).value.strip()
        self.query_one("#config-form-body").disabled = True
        self.query_one("#config-save", Button).disabled = True
        self.query_one("#config-form-status", Static).update(
            "正在查询所选 Docker 环境；不会下载或执行镜像。Esc 可取消。"
        )
        self._docker_task = asyncio.create_task(self._docker_action(action, path, text))

    async def _docker_action(self, action, path, text):
        try:
            policy = self.raw["policy"]
            if action == "docker-refresh":
                key = path[-1]
                choices = (
                    await docker_choices.contexts() if key == "docker_context"
                    else await docker_choices.images(policy["docker_context"])
                )
                self._docker_options[key] = choices
                message = (
                    "列表已刷新；请选择一项并点更新，也可手动填写。"
                    if choices else "未找到可选项；可手动填写已有环境，或在准备好后刷新。"
                )
            elif action == "config-update":
                identity = await docker_choices.resolve_image(policy["docker_context"], text)
                policy["image"] = identity
                message = "镜像已解析并固定；点击校验并保存使用才会写文件。"
            else:
                # Validate the full draft first. UI convenience never weakens
                # the existing pinned-image protocol or stale-file protection.
                validate_document(self.kind, self.raw, self.path)
                await docker_choices.resolve_image(policy["docker_context"], policy["image"])
                self._save()
                return
            self.rebuild()
            self.query_one("#config-form-status", Static).update(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if action == "docker-refresh":
                self._docker_options.pop(path[-1], None)
                self.rebuild()
            self._show_error(error)
        finally:
            self.query_one("#config-form-body").disabled = False
            self.query_one("#config-save", Button).disabled = False

    def _save(self):
        validate_document(self.kind, self.raw, self.path)
        if self.path.exists() and self.path.read_bytes() != self.expected_bytes:
            raise LaunchConfigurationError("文件已有内容或加载后被修改，请重新加载后再保存。")
        atomic_json(self.path, self.raw)
        self.dismiss(self.path)

    def toggle(self):
        path = self.selected_path
        key = path[-1]
        context = self.raw["context"]
        if self.value(path) is None and not any(
            context[name] is not None for name in ("history", "skills", "memory")
        ):
            # An existing empty policy has no reading budget. Show the editable
            # starter budgets when the user explicitly enables its first reader.
            preset = context_preset()["context"]
            for name in ("total_bytes", "item_bytes", "max_blocks", "max_query_bytes"):
                context[name] = preset[name]
        if self.value(path) is not None:
            self.assign(path, None)
            if key == "history":
                self.raw["context"].update(
                    history_tier=None, history_bytes=0, workspace_observations=False
                )
            if key == "memory":
                self.raw["project"] = None
                self.raw["context"]["workspace_observations"] = False
            if key == "skills":
                self.raw["skill_policy"] = None
        elif key == "history":
            self.assign(
                path,
                dict(
                    max_blocks=32,
                    max_depth=32,
                    page_bytes=24000,
                    page_messages=20,
                    max_source_events=10000,
                    max_source_bytes=32000000,
                    max_requests=8,
                ),
            )

            self.raw["context"].update(history_tier="directory", history_bytes=32000)
        elif key == "skills":
            self.assign(path, retrieval_preset())
            self.raw["skill_policy"] = dict(
                limits=dict(
                    max_skills=32,
                    max_catalog_bytes=32000,
                    max_summary_bytes=2000,
                    max_content_bytes=32000,
                    max_resource_bytes=32000,
                ),
                resource_roots=[],
            )
        elif key == "memory":
            self.assign(path, retrieval_preset())
            self.raw["project"] = dict(
                sources={},
                managed_root=str(Path(self.data_dir or ".traceh").resolve() / "managed"),
                limits=dict(max_catalog_events=1000, max_label_bytes=200),
                memory_policy=dict(
                    max_body_bytes=4096,
                    max_sources=8,
                    max_source_events=10000,
                    max_source_bytes=32000000,
                    max_memory_events=10000,
                    denied_patterns=["(?i)api[_ -]?key", "(?i)password", "(?i)token"],
                ),
            )

        if not any(context[name] is not None for name in ("history", "skills", "memory")):
            context.update(history_bytes=0, item_bytes=0, max_blocks=0, max_query_bytes=0)

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        action = event.button.id
        if action == "config-cancel":
            await self.action_back()
            return
        if self.kind == "sandbox" and (
            action in {"docker-refresh", "config-save"}
            or (action == "config-update" and self.selected_path == ("policy", "image"))
        ):
            self._start_docker(action)
            return
        try:
            path = self.selected_path
            value = self.value(path)
            if action == "config-toggle":
                self.toggle()
            elif action == "config-update":
                key = path[-1]
                text = self.query_one("#config-value", Input).value
                choice = self.query_one("#config-choice", Select)
                if choice.display:
                    if choice.is_blank():
                        raise ValueError
                    updated = choice.value
                elif type(value) is int or (value is None and key.startswith("max_")):
                    updated = int(text) if text.strip() else None
                elif type(value) is float:
                    updated = float(text)
                else:
                    updated = text
                if path == ("policy", "docker_context") and self.kind == "sandbox":
                    updated = str(updated).strip()
                    if updated != value:
                        self.raw["policy"]["image"] = ""
                        self._docker_options.pop("image", None)
                self.assign(path, updated)
            elif action == "config-add":
                key = path[-1]
                if isinstance(value, dict):
                    name = self.query_one("#config-new-key", Input).value.strip()
                    if not name or name in value:
                        raise ValueError
                    value[name] = self.workspace if key == "sources" else ""
                else:
                    template = {
                        "plugin_grants": dict(plugin_id="", version="", workspace="",
                                              stdio=dict(input_bytes=1048576, frame_bytes=65536),
                                              max_processes=1),
                        "resource_roots": dict(plugin=dict(plugin_id="", version=""), path=""),
                        "commands": dict(command_id=str(uuid4()), argv=[], timeout_ms=60000),
                    }
                    value.append(template.get(key, ""))
            elif action == "config-remove":
                del self.value(path[:-1])[path[-1]]
                self.selected_path = path[:-1]
            elif action == "config-save":
                self._save()
                return
            self.rebuild()
            self.query_one("#config-form-status", Static).update(
                "草稿已更新；点击校验并保存使用才会写文件。"
            )
        except Exception as error:
            self._show_error(error)

    def _show_error(self, error):
        field = getattr(error, "field", "")
        # Only trusted labels and fixed messages enter the UI; never raw Docker stderr.
        label = " → ".join(LABELS.get(k, "配置项") for k in str(field).split(".")) if field else ""
        message = (
            str(error)
            if isinstance(error, (LaunchConfigurationError, docker_choices.DockerChoiceError))
            else f"{label} 配置未通过校验。检查必填项、数字、仓库路径及命令；现有文件未修改。"
        )
        self.query_one("#config-form-status", Static).update(message)

    async def _stop_docker(self):
        if self._docker_task is not None and not self._docker_task.done():
            self._docker_task.cancel()
            await await_worker_convergence(self._docker_task)

    async def on_unmount(self):
        await self._stop_docker()

    async def action_back(self):
        await self._stop_docker()
        self.dismiss(None)
