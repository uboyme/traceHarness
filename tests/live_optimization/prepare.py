"""Explicit AO-2 example fixtures. No example values are production defaults."""

import copy
import json
from pathlib import Path

from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.variant_execution import write_json


def prepare(source, output):
    source, output = Path(source), Path(output)
    cases = json.loads((source / "dataset.json").read_text(encoding="utf-8"))["cases"]
    selected = [
        c
        for c in cases
        if (c["case_id"], c["material_seed"])
        in {
            ("s-direct", 113),
            ("s-resource", 113),
            ("s-absent", 113),
            ("s-english", 419),
        }
    ]
    base = json.loads((source / "benchmark.json").read_text(encoding="utf-8"))

    def publish(name, material, extras=None):
        root = output / name
        root.mkdir(parents=True, exist_ok=False)
        (root / "rubric.json").write_bytes((source / "rubric.json").read_bytes())
        for case in material:
            for ref in case["setup"].get("resources", []):
                path = root / ref["file"]
                path.parent.mkdir(parents=True, exist_ok=True)
                content = (
                    extras[ref["file"]]
                    if extras and ref["file"] in extras
                    else (source / ref["file"]).read_bytes()
                )
                path.write_bytes(content)
        write_json(
            root / "dataset.json",
            {"format": 1, "purpose": "development-regression", "cases": material},
        )
        manifest = copy.deepcopy(base)
        manifest["benchmark_id"] = "ao2-explicit-" + name
        manifest["dataset"]["sha256"] = digest_bytes((root / "dataset.json").read_bytes())
        write_json(root / "benchmark.json", manifest)

    publish("development", selected)
    holdout = []

    def skill(case_id, title, question, heading, body, value, resource=False):
        descriptor = copy.deepcopy(selected[0]["setup"]["descriptor"])
        descriptor.update(skill_id=case_id + ".manual", title=title, summary=title + "操作说明。")
        descriptors, contents = [], []
        for i in range(10):
            text = f"此为测试场景的常规整理事项 {i}，记录当日清洁完成情况。"
            descriptors.append(
                {
                    "section_id": f"routine-{i}",
                    "tier": "section",
                    "title": f"清洁登记 {i}",
                    "summary": "清洁记录。",
                    "content_digest": digest_bytes(text.encode()),
                    "content_bytes": len(text.encode()),
                }
            )
            contents.append({"section_id": f"routine-{i}", "body": text})
        body_bytes = body.encode()
        descriptor["sections"], descriptor["resources"] = descriptors, []
        if not resource:
            descriptors.append(
                {
                    "section_id": "required-procedure",
                    "tier": "section",
                    "title": heading,
                    "summary": heading + "的正式规定。",
                    "content_digest": digest_bytes(body_bytes),
                    "content_bytes": len(body_bytes),
                }
            )
            contents.append({"section_id": "required-procedure", "body": body})
        descriptors.sort(key=lambda item: item["section_id"])
        contents.sort(key=lambda item: item["section_id"])
        case = {
            "case_id": case_id,
            "group_id": case_id,
            "family": "skill",
            "material_seed": 907,
            "question": question,
            "setup": {"descriptor": descriptor, "sections": contents, "resources": []},
            "expectation": {
                "kind": "value",
                "value": value,
                "value_type": "code",
                "source_text": body,
                "reference_id": descriptor["skill_id"],
            },
        }
        if resource:
            file = f"materials/{case_id}/procedure.txt"
            descriptor["resources"] = [
                {
                    "resource_id": "response-card",
                    "relative_path": "procedure.txt",
                    "content_digest": digest_bytes(body_bytes),
                    "content_bytes": len(body_bytes),
                    "title": heading,
                    "summary": heading + "的步骤和口令。",
                    "chunks": [
                        {
                            "chunk_id": "procedure",
                            "byte_start": 0,
                            "byte_end": len(body_bytes),
                            "content_digest": digest_bytes(body_bytes),
                            "content_bytes": len(body_bytes),
                            "title": heading,
                            "summary": heading + "的步骤。",
                        }
                    ],
                }
            ]
            case["setup"]["resources"] = [{"file": file, "sha256": digest_bytes(body_bytes)}]
            # Publish this new material below; it never enters development observations.
            return case, (file, body_bytes)
        return case, None

    first, _ = skill(
        "mobile-specimen",
        "流动标本车工作手册",
        "流动标本车的接收箱封签应打印哪个代号？",
        "接收箱封签",
        "标本车接收箱的封签必须打印 LIME-274。",
        "LIME-274",
    )
    second, extra = skill(
        "restoration-room",
        "古籍修复室工作手册",
        "古籍修复室遇到烘干柜异常时，要报什么口令并按哪个按钮？",
        "烘干柜异常响应卡",
        "发现烘干柜温湿异常时，值班员应报 RILL-682，并按下蓝色隔离钮。",
        "RILL-682",
        resource=True,
    )
    absent, _ = skill(
        "stamp-catalog",
        "邮票目录整理规范",
        "邮票目录整理规范里写了停车券多少钱吗？",
        "条目登记",
        "目录登记要填写藏品编号和入库日期。",
        "藏品编号",
    )
    absent["expectation"] = {
        "kind": "no-evidence",
        "value": "",
        "value_type": "none",
        "source_text": "",
        "reference_id": "",
    }
    holdout.extend([first, second, absent])
    body = "共享印制车间获批的同时入场上限为 27 人。"
    holdout.append(
        {
            "case_id": "printshop-limit",
            "group_id": "printshop-limit",
            "family": "memory",
            "material_seed": 907,
            "question": "共享印制车间批准的同时入场人数是多少？",
            "setup": {
                "project_id": "printshop",
                "source_id": "printshop-records",
                "label": "共享印制车间",
                "items": [
                    {
                        "memory_id": "printshop.capacity",
                        "fact_slot": "capacity",
                        "body": body,
                        "predecessor_id": None,
                    }
                ],
                "revoke_id": None,
                "second_session": True,
            },
            "expectation": {
                "kind": "value",
                "value": "27",
                "value_type": "number",
                "source_text": body,
                "reference_id": "printshop.capacity",
            },
        }
    )
    history = "临时地质展的新提交批次标志为 KITE-905，交接点设在西拱门第二排。"
    holdout.append(
        {
            "case_id": "geology-transfer",
            "group_id": "geology-transfer",
            "family": "history",
            "material_seed": 907,
            "question": "之前临时地质展说的提交批次标志和交接点是什么？",
            "setup": {
                "turns": ["临时地质展已核对灯具。", history, "今日展柜清洁已登记。"],
                "prompt_prefix": "请记下这条测试记录，只回复已记下：",
                "summary": "本会话记录过展务交接，具体安排在历史原文。",
            },
            "expectation": {
                "kind": "value",
                "value": "KITE-905",
                "value_type": "code",
                "source_text": history,
                "reference_id": "",
            },
        }
    )
    # New resource bytes belong only to the held-out fixture directory.
    publish("holdout", holdout, {extra[0]: extra[1]})
    return output
