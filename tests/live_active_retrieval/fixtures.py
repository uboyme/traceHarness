"""Deterministic synthetic AR-D sources; no production defaults or model instructions."""

import hashlib


def materialize(manifest):
    fixtures = []
    for seed in manifest["repeat_seeds"]:
        for case in manifest["cases"]:
            identity = f"{seed}-{case['id']}"
            suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
            number = 25 + int(suffix, 16) % 50
            value = {
                "code": "REF-" + suffix.upper(),
                "number": str(number),
                "location": f"东侧第{number}号资料柜",
                "none": "",
            }[case["value_type"]]
            source = case["source_body"].format(value=value)
            fixture = {
                **case,
                "seed": seed,
                "identity": identity,
                "value": value,
                "source": source,
                "domain_name": manifest["fixtures"]["domains"][case["domain"]],
                "project_id": "project-" + suffix,
                "source_id": "source-" + suffix,
                "memory_id": "fact-" + suffix,
                "fact_slot": "slot-" + suffix,
                "skill_id": "manual-" + suffix,
                "plugin_id": "reference.author",
            }
            if case["family"] == "history":
                turns = [
                    f"背景登记 {index}：本次活动普通器材已经清点。"
                    for index in range(manifest["fixtures"]["history_background_turns"])
                ]
                if case["mode"] == "disambiguate":
                    source += f" 搬运组的联络代号是 DECOY-{suffix.upper()}。"
                turns[manifest["fixtures"]["history_target_position"]] = source
                fixture["history_turns"] = turns
            elif case["family"] == "skill":
                title = "设备演练手册" if case["domain"] == 0 else "档案整理手册"
                if case["language"] == "en":
                    title = "Equipment drill manual"
                sections = [
                    {
                        "id": f"section-{index:02}",
                        "title": f"普通事项 {index}",
                        "summary": "说明普通器材登记。",
                        "body": "请登记普通器材。",
                    }
                    for index in range(manifest["fixtures"]["skill_noise_sections"] + 1)
                ]
                target = {
                    "id": f"section-{manifest['fixtures']['skill_target_position']:02}",
                    "title": case["source_title"],
                    "summary": case["source_title"],
                    "body": source,
                }
                resources = []
                if case["mode"] == "resource":
                    resources.append(
                        {
                            **target,
                            "id": "resource-" + suffix,
                            "chunk_id": "chunk-" + suffix,
                            "path": "reference.txt",
                        }
                    )
                else:
                    sections[manifest["fixtures"]["skill_target_position"]] = target
                fixture.update(
                    skill_title=title,
                    skill_summary=title + "的工作说明。",
                    sections=sections,
                    resources=resources,
                )
            elif case["family"] == "memory":
                noise = []
                for index in range(manifest["fixtures"]["memory_noise_facts"]):
                    body = f"普通材料保管记录第{index}项：由资料组维护登记。"
                    if case["mode"] == "omitted":
                        body = (
                            f"项目应急联络呼号查询安排第{index}项：询问已批准的项目应急联络"
                            "使用哪个呼号时，请查值班联络批准记录；此条只确认查询分工，不包含呼号值。"
                        )
                    noise.append(
                        {
                            "id": f"background-{suffix}-{index:02}",
                            "slot": f"background-slot-{index:02}",
                            "body": body,
                        }
                    )
                fixture["memory_noise"] = noise
                fixture["predecessor_value"] = (
                    str(number + 100) if case["mode"] == "superseded" else None
                )
            else:
                lines = [
                    f"检查行 {index + 1:04}：普通器材登记，状态 normal。"
                    for index in range(manifest["fixtures"]["output_noise_lines"])
                ]
                target = manifest["fixtures"]["output_target_line"] - 1
                if case["mode"] == "nearby":
                    lines[target] = "诊断分组：以下项目属于该分组。"
                    lines[manifest["fixtures"]["output_detail_line"] - 1] = source
                else:
                    lines[target] = source
                fixture["output_text"] = "\n".join(lines) + "\n"
                fixture["exit_code"] = 7 if case["mode"] == "nonzero" else 0
            fixtures.append(fixture)
    return fixtures
