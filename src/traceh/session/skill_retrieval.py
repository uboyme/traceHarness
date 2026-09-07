"""Eligible-corpus exact/FTS retrieval and immutable Skill block verification."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from fractions import Fraction

from traceh.api.json_types import canonical_json
from traceh.session.context_index import ContextCorpus
from traceh.session.skill_selection import eligible_skills, head_ref, project_selection


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _han(char):
    # Unicode Han unified ideographs and their extension/compatibility blocks.
    value = ord(char)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x2EE5F
        or 0x2F800 <= value <= 0x2FA1F
        or 0x30000 <= value <= 0x323AF
    )


def tokenize(text):
    result, run, han = [], "", False

    def flush():
        if han:
            result.extend(run)
            result.extend(run[i : i + 2] for i in range(len(run) - 1))
        elif run:
            result.append(run)

    for char in normalize(text):
        current_han = _han(char)
        if not char.isalnum():
            flush()
            run, han = "", False
        else:
            if run and current_han != han:
                flush()
                run = ""
            han = current_han
            run += char
    flush()
    return tuple(result)


def block_identity(block):
    p = block["provenance"]
    return canonical_json(
        [
            block["kind"],
            block["id"],
            block["version"],
            block["tier"],
            p["section_id"],
            p["resource_id"],
            p["chunk_id"],
            block["content_digest"],
        ]
    )


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
        body = canonical_json(
            {
                key: descriptor.to_dict()[key]
                for key in ("skill_id", "version", "plugin", "title", "tags")
            }
        )
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
        body = (
            descriptor.summary
            if tier == "summary"
            else canonical_json(
                {
                    key: descriptor.to_dict()[key]
                    for key in ("skill_id", "version", "plugin", "title", "tags")
                }
            )
        )
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


def _matches_exact(value, normalized_query):
    # Keep the complete literal, including spaces, within the existing identifier boundaries.
    pattern = rf"(?<![\w./\\:#-]){re.escape(normalize(value))}(?![\w./\\:#-])"
    return re.search(pattern, normalized_query) is not None


def rank(blocks, rows, descriptors, query, fts_hits, policy):
    terms = tuple(sorted(set(tokenize(query))))
    if len(terms) > policy.max_terms:
        raise ValueError("skill-query-resource-limit")
    exact = []
    normalized_query = normalize(query)
    for block, descriptor in zip(blocks, descriptors, strict=True):
        values = {
            "id": [descriptor.skill_id],
            "tag": descriptor.tags,
            "path": [r.relative_path for r in descriptor.resources],
            "symbol": re.findall(r"[\w.:/-]+", descriptor.summary),
            "error": re.findall(r"[\w-]+", descriptor.summary),
        }
        priority = next(
            (
                i
                for i, field in enumerate(policy.match_fields)
                if any(_matches_exact(value, normalized_query) for value in values[field])
            ),
            None,
        )
        if priority is not None:
            exact.append((priority, block_identity(block)))
    exact_ids = [identity for _, identity in sorted(exact)]
    lexical = []
    if fts_hits is not None and rows:
        expected = {row["identity"] for row in rows if set(row["tf"]) & set(terms)}
        if set(fts_hits) != expected:
            fts_hits = None
        else:
            count = len(rows)
            average = sum(row["length"] for row in rows) / count
            df = {term: sum(term in row["tf"] for row in rows) for term in terms}
            for row in rows:
                if row["identity"] not in expected:
                    continue
                score = sum(
                    math.log(1 + (count - df[term] + 0.5) / (df[term] + 0.5))
                    * row["tf"][term]
                    * (policy.k1 + 1)
                    / (
                        row["tf"][term]
                        + policy.k1 * (1 - policy.b + policy.b * row["length"] / average)
                    )
                    for term in terms
                    if term in row["tf"]
                )
                lexical.append((-score, row["identity"]))
    fts_ids = [identity for _, identity in sorted(lexical)]
    unavailable = []
    if fts_hits is None:
        unavailable.append("index-unavailable")
    lanes = [(exact_ids, policy.exact_weight), (fts_ids, policy.fts_weight)]
    lane_receipts = []
    for name, ids, raw_scores in (
        ("exact", exact_ids, {identity: priority for priority, identity in exact}),
        ("fts", fts_ids, {identity: -score for score, identity in lexical}),
    ):
        status = "available"
        if name == "fts" and fts_hits is None:
            status = "index-unavailable"
        if len(ids) > policy.max_candidates:
            status = "resource-limit"
        lane_receipts.append(
            {
                "id": name,
                "status": status,
                "ranking": [
                    {"identity": identity, "score": raw_scores[identity]} for identity in ids
                ]
                if status == "available"
                else [],
            }
        )
    scores = {}
    for ids, weight in lanes:
        if len(ids) > policy.max_candidates:
            unavailable.append("resource-limit")
            continue
        for ordinal, identity in enumerate(ids, 1):
            scores[identity] = scores.get(identity, Fraction()) + Fraction(
                weight, policy.rrf_constant + ordinal
            )
    ordered = sorted(scores, key=lambda identity: (-scores[identity], identity))
    if len(ordered) > policy.max_candidates:
        return [], [*unavailable, "resource-limit"], {"lanes": lane_receipts, "fusion": []}
    by_identity = {block_identity(block): block for block in blocks}
    receipt = {
        "lanes": lane_receipts,
        "fusion": [
            {
                "identity": identity,
                "numerator": scores[identity].numerator,
                "denominator": scores[identity].denominator,
            }
            for identity in ordered
        ],
    }
    return [by_identity[identity] for identity in ordered], unavailable, receipt


def validate_retrieval_receipt(receipt, policy):
    """Validate frozen lane/fusion evidence without querying any index at replay."""
    if receipt is None:
        return
    if (
        policy is None
        or type(receipt) is not dict
        or set(receipt)
        != {"format", "corpus_key", "corpus_digest", "eligible_count", "lanes", "fusion"}
        or type(receipt["format"]) is not int
        or receipt["format"] != 1
    ):
        raise ValueError("skill-retrieval-receipt-invalid")
    for name in ("corpus_key", "corpus_digest"):
        value = receipt[name]
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("skill-retrieval-receipt-invalid")
    if (
        type(receipt["eligible_count"]) is not int
        or not 0 <= receipt["eligible_count"] <= policy.max_corpus_items
    ):
        raise ValueError("skill-retrieval-receipt-invalid")
    lanes = receipt["lanes"]
    if type(lanes) is not list or len(lanes) != 2:
        raise ValueError("skill-retrieval-receipt-invalid")
    scores = {}
    for lane, name, weight in zip(
        lanes, ("exact", "fts"), (policy.exact_weight, policy.fts_weight), strict=True
    ):
        if (
            type(lane) is not dict
            or set(lane) != {"id", "status", "ranking"}
            or lane["id"] != name
            or lane["status"] not in {"available", "resource-limit", "index-unavailable"}
        ):
            raise ValueError("skill-retrieval-receipt-invalid")
        ranking = lane["ranking"]
        if (
            type(ranking) is not list
            or len(ranking) > policy.max_candidates
            or (lane["status"] != "available" and ranking)
        ):
            raise ValueError("skill-retrieval-receipt-invalid")
        seen, order = set(), []
        for ordinal, item in enumerate(ranking, 1):
            if (
                type(item) is not dict
                or set(item) != {"identity", "score"}
                or type(item["identity"]) is not str
                or type(item["score"]) not in {int, float}
                or not math.isfinite(item["score"])
                or item["score"] < 0
                or item["identity"] in seen
            ):
                raise ValueError("skill-retrieval-receipt-invalid")
            seen.add(item["identity"])
            order.append((item["score"] if name == "exact" else -item["score"], item["identity"]))
            scores[item["identity"]] = scores.get(item["identity"], Fraction()) + Fraction(
                weight, policy.rrf_constant + ordinal
            )
        if order != sorted(order):
            raise ValueError("skill-retrieval-rank-invalid")
    expected = [
        {
            "identity": identity,
            "numerator": scores[identity].numerator,
            "denominator": scores[identity].denominator,
        }
        for identity in sorted(scores, key=lambda identity: (-scores[identity], identity))
    ]
    if len(expected) > policy.max_candidates:
        expected = []
    if canonical_json(receipt["fusion"]) != canonical_json(expected):
        raise ValueError("skill-retrieval-fusion-invalid")


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
