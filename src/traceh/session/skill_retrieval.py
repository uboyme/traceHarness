"""Eligible-corpus exact/FTS retrieval and immutable Skill block verification."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from traceh.api.json_types import canonical_json
from traceh.session.context_index import ContextCorpus
from traceh.session.retrieval import block_identity, tokenize
from traceh.session.skill_selection import eligible_skills, head_ref, project_selection


def make_block(
    descriptor,
    tier,
    session_id,
    catalog_digest,
    *,
    reader=None,
    section_id=None,
    resource_id=None,
    chunk_id=None,
):
    if tier == "directory":
        body = canonical_json(descriptor.directory())
    elif tier == "summary":
        body = descriptor.summary
    elif tier == "section":
        body = reader.read_section(descriptor.skill_id, section_id).decode("utf-8")
    elif tier == "chunk":
        body = reader.read_chunk(descriptor.skill_id, resource_id, chunk_id).decode("utf-8")
    else:
        raise ValueError("skill-disclosure-tier-invalid")
    result = {
        "kind": "skill",
        "id": descriptor.skill_id,
        "version": descriptor.version,
        "tier": tier,
        "scope": {"kind": "session", "session_id": session_id, "project_binding": None},
        "source_refs": [],
        "body": body,
        "content_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "content_bytes": len(body.encode("utf-8")),
        "provenance": {
            "plugin": descriptor.plugin.to_dict(),
            "skill_id": descriptor.skill_id,
            "section_id": section_id,
            "resource_id": resource_id,
            "chunk_id": chunk_id,
            "catalog_digest": catalog_digest,
        },
    }
    verify_block(result, descriptor, catalog_digest)
    return result


def verify_block(block, descriptor, catalog_digest):
    p = block["provenance"]
    if (
        set(p) != {"plugin", "skill_id", "section_id", "resource_id", "chunk_id", "catalog_digest"}
        or p["plugin"] != descriptor.plugin.to_dict()
        or p["skill_id"] != descriptor.skill_id
        or block["id"] != descriptor.skill_id
        or block["version"] != descriptor.version
        or p["catalog_digest"] != catalog_digest
        or block["source_refs"] != []
    ):
        raise ValueError("skill-block-catalog-mismatch")
    tier = block["tier"]
    if tier in {"directory", "summary"}:
        if any(p[key] is not None for key in ("section_id", "resource_id", "chunk_id")):
            raise ValueError("skill-block-source-mismatch")
        body = descriptor.summary if tier == "summary" else canonical_json(descriptor.directory())
        if block["body"] != body:
            raise ValueError("skill-block-content-mismatch")
    elif tier == "section":
        source = next((s for s in descriptor.sections if s.section_id == p["section_id"]), None)
        if source is None or p["resource_id"] is not None or p["chunk_id"] is not None:
            raise ValueError("skill-block-source-mismatch")
        if (
            source.content_digest != block["content_digest"]
            or source.content_bytes != block["content_bytes"]
        ):
            raise ValueError("skill-block-content-mismatch")
    elif tier == "chunk":
        resource = next(
            (r for r in descriptor.resources if r.resource_id == p["resource_id"]), None
        )
        source = (
            next((c for c in resource.chunks if c.chunk_id == p["chunk_id"]), None)
            if resource
            else None
        )
        if source is None or p["section_id"] is not None:
            raise ValueError("skill-block-source-mismatch")
        if (
            source.content_digest != block["content_digest"]
            or source.content_bytes != block["content_bytes"]
        ):
            raise ValueError("skill-block-content-mismatch")
    else:
        raise ValueError("skill-disclosure-tier-invalid")


def prepare_corpus(composition, selections, session_id, policy):
    selection = project_selection(selections, session_id)
    descriptors, reason = eligible_skills(selection, composition)
    if (
        len(canonical_json(composition.to_dict()["skill_catalog"]).encode("utf-8"))
        > policy.max_catalog_bytes
    ):
        raise ValueError("skill-catalog-resource-limit")
    blocks, rows = [], []
    for descriptor in descriptors:
        block = make_block(
            descriptor, policy.default_tier, session_id, composition.skill_catalog_digest
        )
        # Ranking sees only selected canonical metadata, never undisclosed section/resource bytes.
        terms = tokenize(
            " ".join((descriptor.skill_id, descriptor.title, descriptor.summary, *descriptor.tags))
        )
        frequencies = dict(sorted(Counter(terms).items()))
        rows.append(
            {
                "identity": block_identity(block),
                "kind": "skill",
                "id": descriptor.skill_id,
                "version": descriptor.version,
                "tier": block["tier"],
                "scope": block["scope"],
                "source_refs": [],
                "content_digest": block["content_digest"],
                "tf": frequencies,
                "length": len(terms),
                "terms": " ".join("t" + term.encode("utf-8").hex() for term in terms),
            }
        )
        blocks.append(block)
    if (
        len(rows) > policy.max_corpus_items
        or len(canonical_json(rows).encode("utf-8")) > policy.max_corpus_bytes
    ):
        raise ValueError("skill-corpus-resource-limit")
    corpus = ContextCorpus.build(
        scope={"kind": "session", "session_id": session_id, "project_binding": None},
        catalog_digest=composition.skill_catalog_digest,
        source_heads=[head_ref(session_id, selections)],
        config_digest=policy.digest,
        items=rows,
    )
    return corpus, blocks, rows, descriptors, reason


def verify_retrieval_catalog(receipt, composition, session_id, policy):
    if receipt is None:
        return
    identities = {
        block_identity(
            make_block(
                descriptor, policy.default_tier, session_id, composition.skill_catalog_digest
            )
        )
        for descriptor in composition.skill_catalog
    }
    if any(
        item["identity"] not in identities for lane in receipt["lanes"] for item in lane["ranking"]
    ):
        raise ValueError("skill-retrieval-catalog-mismatch")


def receipt_skill_ids(receipt):
    if receipt is None:
        return ()
    return tuple(
        (json.loads(item["identity"])[1], json.loads(item["identity"])[2])
        for lane in receipt["lanes"]
        for item in lane["ranking"]
    )


def exact_values(descriptors):
    return [
        {
            "id": [d.skill_id],
            "tag": d.tags,
            "path": [r.relative_path for r in d.resources],
            "symbol": re.findall(r"[\w.:/-]+", d.summary),
            "error": re.findall(r"[\w-]+", d.summary),
        }
        for d in descriptors
    ]
