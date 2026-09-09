"""Explicit synthetic RE development/holdout materials; never production defaults."""

import copy
import hashlib

from live_active_retrieval.fixtures import materialize


def specimen(
    manifest,
    identity,
    family,
    question,
    source,
    *,
    expected="value",
    topic="陶艺展览",
    mode="direct",
    value_type="code",
    position=417,
):
    """Reuse real source setup shapes, then supply this experiment's explicit material."""
    case = {
        "id": identity,
        "family": family,
        "domain": 0,
        "mode": mode,
        "language": "zh",
        "question": question,
        "source_title": topic + "记录",
        "source_body": source,
        "expected": expected,
        "value_type": value_type,
        "control": False,
        "discovery": mode == "omitted",
    }
    m = copy.deepcopy(manifest)
    m.update(repeat_seeds=[1609], cases=[case])
    fixture = materialize(m)[0]
    fixture.update(id=identity, identity=identity, domain_name=topic, grading=expected)
    value = fixture["value"]
    if family == "output":
        lines = [f"条目 {i}: {topic}例行登记完成。" for i in range(600)]
        lines[position] = source.format(value=value)
        fixture["output_text"] = "\n".join(lines) + "\n"
    elif family == "skill":
        fixture.update(skill_title=topic + "手册", skill_summary=topic + "流程与注意事项。")
        for i, item in enumerate(fixture["sections"]):
            item.update(title=f"{topic}日常事项{i}", summary="记录日常准备。", body="材料已清点。")
        fixture["sections"][position % len(fixture["sections"])].update(
            title="交接记录", summary="交接事项与交班记录。", body=fixture["source"]
        )
    elif family == "history":
        fixture["history_turns"] = [f"{topic}准备记录{i}：已检查照明。" for i in range(10)]
        fixture["history_turns"][position % 10] = fixture["source"]
    elif family == "memory":
        for i, item in enumerate(fixture["memory_noise"]):
            item["body"] = (
                f"{topic}查询分工第{i}项：{question}请查交接批准记录；本条不含具体值。"
                if mode == "omitted"
                else f"{topic}日常准备第{i}项由值班组登记。"
            )
    return fixture


def controls(manifest, prefix="control"):
    requests = [
        ("greeting", "你好！", ""),
        ("rewrite", "把“请把门关上”改得更礼貌一点。", ""),
        ("sum", "28 加 35 等于多少？", "63"),
        ("repeat", "我现在告诉你参加者有 42 人，重复一下这个人数。", "42"),
    ]
    if prefix == "hold-control":
        requests = [
            ("greeting", "早上好。", ""),
            ("rewrite", "把“把包递给我”改为礼貌的请求。", ""),
            ("sum", "19 加 47 是多少？", "66"),
            ("repeat", "这次准备了 58 个空盒，请重复一下盒子数量。", "58"),
        ]
    result = []
    for suffix, question, answer in requests:
        f = specimen(
            manifest,
            prefix + "-" + suffix,
            "skill",
            question,
            "此手册记录陶泥存放要求。",
            expected="direct-control",
            value_type="none",
        )
        f.update(value=answer, control=True, grading="direct-control", expected_answer=answer)
        result.append(f)
    return result


def development(manifest):
    def make(*args, **kwargs):
        return specimen(manifest, *args, **kwargs)

    groups = {
        "re1": [
            make(
                "re1-business-zh",
                "output",
                "刚才归档检查返回的那串核验代号是什么？",
                "归档核验代号={value}",
                topic="河岸档案整理",
            ),
            make(
                "re1-business-other",
                "output",
                "陶艺验收的回执编号是什么？",
                "验收回执编号={value}",
                position=283,
            ),
            make(
                "re1-business-en",
                "output",
                "What validation code did the equipment check return earlier?",
                "validation code={value}",
                topic="器材巡检",
            ),
            make(
                "re1-integrity",
                "output",
                "刚才保存的工具输出，其内容校验指纹 digest 是什么？",
                "样品记录已保存。",
                expected="integrity",
                value_type="none",
            ),
            make(
                "re1-process",
                "output",
                "刚才命令的进程退出码是多少？",
                "本次检测结束。",
                expected="exit-code",
                value_type="none",
                mode="nonzero",
            ),
            make(
                "re1-absent",
                "output",
                "刚才陶艺验收给出的回执编号是什么？",
                "本次仅完成清点，未生成验收回执编号。",
                expected="no-evidence",
                value_type="none",
            ),
        ],
        "re2": [
            make(
                "re2-omitted",
                "memory",
                "项目应急联络使用哪个呼号？我问的是已批准的安排。",
                "已批准的项目应急联络呼号是 {value}。",
                mode="omitted",
            ),
            make(
                "re2-omitted-other",
                "memory",
                "花卉展的备用集合点批准在哪里？",
                "交接批准记录：备用集合点为 {value}。",
                mode="omitted",
                topic="花卉展",
                value_type="location",
            ),
            make(
                "re2-direct",
                "memory",
                "陶艺展览当前批准的交接代码是什么？",
                "陶艺展览已批准交接代码为 {value}。",
            ),
            make(
                "re2-revoked",
                "memory",
                "陶艺展览现在有有效批准的备用口令吗？",
                "曾批准备用口令为 {value}。",
                mode="revoked",
                expected="no-evidence",
            ),
            make(
                "re2-absent",
                "memory",
                "项目批准了停车优惠码吗？",
                "本项目仅批准物料清点安排。",
                expected="no-evidence",
                value_type="none",
            ),
            make(
                "re2-unbound",
                "memory",
                "当前项目批准的交接口令是什么？",
                "隔离材料不应在未绑定会话中被读取。",
                mode="unbound",
                expected="unavailable",
                value_type="none",
            ),
        ],
        "re3": [
            make(
                "re3-language",
                "output",
                "刚才失败的归档检查给出的故障代号是什么？",
                "故障代号={value}",
                mode="nonzero",
            ),
            make(
                "re3-phrase",
                "output",
                "刚才那次展览检查的核对凭据是什么？",
                "核对凭据编号：{value}",
                position=256,
            ),
            make("re3-direct", "output", "检查输出里的样品序列号是什么？", "样品序列号={value}"),
            make(
                "re3-overlap",
                "output",
                "本次检查的核验编号（核验码）是什么？",
                "核验编号/核验码={value}",
                position=192,
            ),
            make(
                "re3-absent",
                "output",
                "检查输出里有停车许可证编号吗？",
                "仅完成材料检查，没有生成停车许可证编号。",
                expected="no-evidence",
                value_type="none",
            ),
            make(
                "re3-nearby",
                "output",
                "诊断组乙的交接凭据是什么？",
                "交接凭据={value}",
                position=488,
            ),
        ],
        "re4": [],
    }
    nearby = groups["re3"][-1]
    lines = nearby["output_text"].splitlines()
    lines[478] = "诊断组乙：接下来是该组的记录。"
    nearby["output_text"] = "\n".join(lines) + "\n"
    for family in ["skill", "history", "memory"]:
        for positive in [True, False]:
            q = "陶艺交接时要使用哪张凭证？"
            source = "交接凭证是 {value}。" if positive else "本次交接不使用凭证。"
            f = make(
                f"re4-{family}-{'yes' if positive else 'no'}",
                family,
                q,
                source,
                mode="omitted" if family == "memory" else "direct",
                expected="value" if positive else "no-evidence",
            )
            groups["re4"].append(f)
    return groups


def holdout(manifest):
    """Different topics, layout and questions; frozen before any candidate result."""
    result = []
    topics = ["温室巡检", "展馆布展"]
    for group in range(1, 5):
        for index in range(4):
            topic = topics[index % 2]
            identity = f"hold-re{group}-{index}"
            family = {
                1: "output",
                2: "memory",
                3: "output",
                4: "history" if index % 2 else "skill",
            }[group]
            positive = index < 2
            q = f"{topic}交班时，应使用哪个封签批次？"
            source = (
                f"{topic}交班封签批次是 {{value}}。" if positive else f"{topic}本次交班不使用封签。"
            )
            expected = "value" if positive else "no-evidence"
            mode = "omitted" if group == 2 else "direct"
            if group == 1 and index == 2:
                q, source, expected = (
                    "刚才保存的输出 digest 是什么？",
                    "完成普通登记。",
                    "integrity",
                )
            if group == 1 and index == 3:
                q, source, expected, mode = (
                    "这次程序是以哪个进程退出码结束的？",
                    "检查完毕。",
                    "exit-code",
                    "nonzero",
                )
            if group == 3 and index == 1:
                q, source = (
                    "What batch identifier was reported for the handover seals?",
                    "交班封签批次为 {value}。",
                )
            f = specimen(
                manifest,
                identity,
                family,
                q,
                source,
                topic=topic,
                mode=mode,
                expected=expected,
                position=51 + index * 127 + group,
            )
            if family == "output":
                f["output_text"] = (
                    f["output_text"]
                    .replace("条目", "盘点事项")
                    .replace("例行登记完成", "已复核包装与件数")
                )
            f["experiment_group"] = f"re{group}"
            result.append(f)
    result.extend(controls(manifest, "hold-control"))
    for index, family in enumerate(["history", "memory", "skill", "output"]):
        f = specimen(
            manifest,
            "hold-protect-" + family,
            family,
            "温室巡检记录的交班集合点在哪里？",
            "交班集合点为 {value}。",
            topic="温室巡检",
            value_type="location",
            position=12 + index,
        )
        f["protection"] = True
        result.append(f)
    return result


def frozen_materials(manifest):
    result = {
        "development": development(manifest),
        "controls": controls(manifest),
        "holdout": holdout(manifest),
    }
    flat = (
        [f for items in result["development"].values() for f in items]
        + result["controls"]
        + result["holdout"]
    )
    assert len({f["identity"] for f in flat}) == len(flat) == 52
    for f in flat:
        if f["grading"] == "value":
            assert f["value"] and f["value"] not in f["question"]
        f["material_digest"] = hashlib.sha256(repr(sorted(f.items())).encode("utf-8")).hexdigest()
    return result
