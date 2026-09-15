"""Explicit real-source diagnostic fixtures; never runtime defaults."""

import ast
import hashlib
import json
from pathlib import Path

from live_dynamic_collaboration.materials import write
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest

SOURCE_FILES = (
    "src/traceh/product/runtime.py",
    "src/traceh/product/resources.py",
    "src/traceh/product/registry.py",
    "src/traceh/supervision/delegation.py",
    "src/traceh/supervision/authority.py",
    "src/traceh/supervision/provisioning.py",
    "src/traceh/supervision/execution.py",
    "src/traceh/supervision/lifecycle.py",
    "src/traceh/supervision/delivery.py",
    "src/traceh/supervision/delivery_service.py",
    "src/traceh/supervision/reports.py",
    "src/traceh/budgets/supervision.py",
    "src/traceh/budgets/enforcement.py",
    "src/traceh/budgets/service.py",
    "src/traceh/budgets/projection.py",
    "src/traceh/workflow/execution.py",
    "src/traceh/workspaces/policy.py",
    "src/traceh/api/budgets.py",
)
COMPLEX = (
    "评审此代码快照中的任务执行边界，交付 answer.json。先读取 INDEX.md，随后根据实际代码分别核对："
    "(1) 主方与调查方的权限、工作区版本和历史继承；(2) 子任务预算如何预留、主方如何保留收尾额度；"
    "(3) 创建/发送取消、清理失败以及主方结束时，怎样确保不会留下运行中的工作，产物何时捕获。"
    "最终综合三个方面说明它们是否自洽及实际限制，每个方面至少引用两处相关代码。"
    '输出格式为 {"sections":[{"topic":"authority"或"budget"或"lifecycle",'
    '"conclusion":"你的分析","evidence":[{"path":"文件相对路径",'
    '"start_line":1,"end_line":2}]}]}。每个 topic 恰好一次。'
    "只能新增 answer.json；这是源码静态评审，不要安装依赖或声称运行了未运行的项目测试。"
    "可运行小范围输出检查，证据不足要如实说明。"
)
EXPLICIT = (
    " 本次明确要求：实际读取 INDEX.md 之后，用 delegate_investigation 派一个只读助手"
    "核对预算和取消边界；主方自己核对权限与来源边界。在使用助手结论前用 collect_investigation"
    "读取其确切消息报告，随后由主方综合交付。这是本次任务的明确工作安排。"
)


def verifier(files, *, version=None):
    expected = {name: digest_bytes(body) for name, body in files.items()}
    source = (
        "import hashlib,json\nfrom pathlib import Path\n"
        f"expected={expected!r}\n"
        "assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h "
        "for n,h in expected.items())\n"
        "extras={p.as_posix() for p in Path('.').rglob('*') "
        "if p.is_file() and '.git' not in p.parts}"
        "-set(expected)\nassert extras=={'answer.json'}, extras\n"
        "answer=json.loads(Path('answer.json').read_text(encoding='utf-8'))\n"
    )
    if version is not None:
        return source + f"assert answer=={{'version':{version!r}}}\n"
    return source + (
        "assert set(answer)=={'sections'} and len(answer['sections'])==3\n"
        "assert {s['topic'] for s in answer['sections']}=={'authority','budget','lifecycle'}\n"
        "for section in answer['sections']:\n"
        " assert isinstance(section['conclusion'],str) and len(section['conclusion'])>=40\n"
        " assert len(section['evidence'])>=2\n"
        " for ref in section['evidence']:\n"
        "  assert ref['path'] in expected and ref['path'].endswith('.py')\n"
        "  lines=Path(ref['path']).read_text(encoding='utf-8').splitlines()\n"
        "  assert type(ref['start_line']) is int and type(ref['end_line']) is int\n"
        "  assert 1<=ref['start_line']<=ref['end_line']<=len(lines)\n"
    )


def build(repository: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    template = json.loads(
        (repository / "benchmarks/dynamic_collaboration_v1/development/benchmark.json").read_text(
            encoding="utf-8"
        )
    )
    settings = template["task_settings"]
    settings.update(
        profile_id="da-autonomy-diagnostic", default_mode="adaptive", modes=["adaptive"]
    )
    corpus = {name: (repository / name).read_bytes() for name in SOURCE_FILES}
    corpus["INDEX.md"] = (
        "# 代码快照评审\n\n这是当前仓库实际源码的选定模块，按原路径保留。\n"
        "product：装配和资源绑定；supervision：创建、投递、执行和收尾；"
        "budgets：预留与结算；workflow：产物前收尾；workspaces：访问约束。\n"
        "这里不是完整可安装项目，请用静态代码和行号做证据，不需要安装依赖。\n"
    ).encode()
    version_bytes = (repository / "src/traceh/version.py").read_bytes()
    version = next(
        ast.literal_eval(n.value)
        for n in ast.parse(version_bytes).body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)
    )
    small = {"src/traceh/version.py": version_bytes}
    cases = []
    for name, requirement, files, actual_version in (
        ("explicit", COMPLEX + EXPLICIT, corpus, None),
        ("natural", COMPLEX, corpus, None),
        (
            "simple",
            "读取 src/traceh/version.py，将代码中的当前版本写入 answer.json，格式为 "
            '{"version":"实际版本"}。只新增这一个文件，完成后检查输出。',
            small,
            version,
        ),
    ):
        initial = output / name / "initial"
        for relative, body in files.items():
            path = initial / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        check = verifier(files, version=actual_version)
        cases.append(
            dict(
                case_id=name,
                group_id=name,
                requirement=requirement,
                initial_tree=initial.relative_to(output).as_posix(),
                sha256=initial_tree_digest(capture_initial_tree(initial)),
                verification=dict(
                    plan_id="diagnostic-" + name,
                    plan_version=1,
                    protocol_version=3,
                    commands=[
                        dict(
                            command_id="output-contract",
                            public_requirement=None,
                            argv=["python", "-B", "-c", check],
                            timeout_ms=30000,
                        )
                    ],
                    environment=dict(
                        policy_id="diagnostic-no-dependencies",
                        passthrough=[],
                        overrides={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
                    ),
                    max_output_bytes=1048576,
                ),
            )
        )
    write(output / "dataset.json", dict(format=2, cases=cases))
    template.update(
        benchmark_id="traceh-autonomy-diagnostic-v1",
        dataset=dict(
            file="dataset.json", sha256=digest_bytes((output / "dataset.json").read_bytes())
        ),
        assessment=dict(
            scorer_id="product-durable-v1", version=1, requires_review=False, rubric=None
        ),
    )
    write(output / "benchmark.json", template)
    write(
        output / "provenance.json",
        dict(
            format=1,
            origin="verbatim selected current repository files",
            files={name: hashlib.sha256(body).hexdigest() for name, body in corpus.items()},
            code_bytes=sum(len(body) for name, body in corpus.items() if name.endswith(".py")),
            code_lines=sum(
                len(body.splitlines()) for name, body in corpus.items() if name.endswith(".py")
            ),
            structural_verification_only=True,
        ),
    )
    return output
