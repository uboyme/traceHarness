"""Shared eligible-corpus exact/FTS ranking and deterministic receipt validation."""

import math
import re
import unicodedata
from fractions import Fraction

from traceh.api.json_types import canonical_json


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


def _word_tokens(text):
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


def _literal_spans(text):
    # Preserve complete code runs, while recognizing prose punctuation. NFKC
    # makes a Chinese colon ASCII too; a Han sentence joined only by a colon
    # must not become a mandatory code anchor. Quotes explicitly opt into a
    # literal interpretation. Compound paths/identifiers keep their connectors.
    result = []
    for match in re.finditer(r"[\w./\\:#-]+", text):
        value = match[0]
        connectors = set(value).intersection("_./\\:#-")
        if not connectors or not any(char.isalnum() for char in value):
            continue
        quoted = (
            match.start() > 0
            and match.end() < len(text)
            and text[match.start() - 1] == text[match.end()]
            and text[match.end()] in "\"'`"
        )
        prose = (connectors == {"."} and value.endswith(".") and value[:-1].isalnum()) or (
            connectors == {":"}
            and ((value.endswith(":") and value[:-1].isalnum()) or any(_han(c) for c in value))
        )
        if quoted or not prose:
            result.append(match)
    return tuple(result)


def tokenize(text):
    """Index ordinary words as well as complete code literals, without reading hidden bodies."""
    normalized = normalize(text)
    return (*_word_tokens(normalized), *(match[0] for match in _literal_spans(normalized)))


def query_terms(text):
    """A literal query never falls back to its component words."""
    normalized = normalize(text)
    spans = _literal_spans(normalized)
    prose, cursor = [], 0
    for match in spans:
        prose.append(normalized[cursor : match.start()])
        cursor = match.end()
    prose.append(normalized[cursor:])
    return tuple(sorted(set((*_word_tokens(" ".join(prose)), *(match[0] for match in spans)))))


def block_identity(block):
    p = block["provenance"]
    if block["kind"] == "history":
        return canonical_json(
            [block["kind"], block["id"], block["version"], block["tier"],
             None if p["page"] is None else p["page"]["index"], block["content_digest"]]
        )
    return canonical_json(
        [
            block["kind"],
            block["id"],
            block["version"],
            block["tier"],
            p.get("section_id"),
            p.get("resource_id"),
            p.get("chunk_id"),
            block["content_digest"],
        ]
    )


def _matches_exact(value, normalized_query):
    # Keep the complete literal, including spaces, within the existing identifier boundaries.
    pattern = rf"(?<![\w./\\:#-]){re.escape(normalize(value))}(?![\w./\\:#-])"
    return re.search(pattern, normalized_query) is not None


def match_coverage(rows, exact_values, query, policy):
    terms = set(query_terms(query))
    normalized_query = normalize(query)
    result = {}
    for row, values in zip(rows, exact_values, strict=True):
        covered = terms.intersection(row["tf"])
        # Resource paths are exact metadata, intentionally absent from the Skill FTS corpus.
        for field in policy.match_fields:
            for value in values[field]:
                if _matches_exact(value, normalized_query):
                    covered.update(terms.intersection(query_terms(value)))
        result[row["identity"]] = sorted(covered)
    return result


def validate_coverage(receipt, rows, exact_values, query, policy):
    """Prove frozen coverage from the same historical source; never rerun an index."""
    expected = match_coverage(rows, exact_values, query, policy)
    if any(expected.get(item["identity"]) != item["terms"] for item in receipt["coverage"]):
        raise ValueError("retrieval-coverage-source-mismatch")


def rank(blocks, rows, exact_values, query, fts_hits, policy):
    terms = query_terms(query)
    if len(terms) > policy.max_terms:
        raise ValueError("retrieval-query-resource-limit")
    exact = []
    normalized_query = normalize(query)
    anchors = {match[0] for match in _literal_spans(normalized_query)}
    coverage = match_coverage(rows, exact_values, query, policy)
    qualified = {
        identity
        for identity, covered in coverage.items()
        if not anchors or anchors.intersection(covered)
    }
    for block, values in zip(blocks, exact_values, strict=True):
        if block_identity(block) not in qualified:
            continue
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
                if row["identity"] not in expected or row["identity"] not in qualified:
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
        return (
            [],
            [*unavailable, "resource-limit"],
            {"lanes": lane_receipts, "fusion": [], "coverage": []},
        )
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
        "coverage": [{"identity": identity, "terms": coverage[identity]} for identity in ordered],
    }
    return [by_identity[identity] for identity in ordered], unavailable, receipt


def validate_retrieval_receipt(receipt, policy, query):
    """Validate frozen lane/fusion evidence without querying any index at replay."""
    if receipt is None:
        return
    if (
        policy is None
        or type(receipt) is not dict
        or set(receipt)
        != {
            "format",
            "corpus_key",
            "corpus_digest",
            "eligible_count",
            "lanes",
            "fusion",
            "coverage",
        }
        or type(receipt["format"]) is not int
        or receipt["format"] != 2
    ):
        raise ValueError("retrieval-receipt-invalid")
    for name in ("corpus_key", "corpus_digest"):
        value = receipt[name]
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("retrieval-receipt-invalid")
    if (
        type(receipt["eligible_count"]) is not int
        or not 0 <= receipt["eligible_count"] <= policy.max_corpus_items
    ):
        raise ValueError("retrieval-receipt-invalid")
    lanes = receipt["lanes"]
    if type(lanes) is not list or len(lanes) != 2:
        raise ValueError("retrieval-receipt-invalid")
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
            raise ValueError("retrieval-receipt-invalid")
        ranking = lane["ranking"]
        if (
            type(ranking) is not list
            or len(ranking) > policy.max_candidates
            or (lane["status"] != "available" and ranking)
        ):
            raise ValueError("retrieval-receipt-invalid")
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
                raise ValueError("retrieval-receipt-invalid")
            seen.add(item["identity"])
            order.append((item["score"] if name == "exact" else -item["score"], item["identity"]))
            scores[item["identity"]] = scores.get(item["identity"], Fraction()) + Fraction(
                weight, policy.rrf_constant + ordinal
            )
        if order != sorted(order):
            raise ValueError("retrieval-rank-invalid")
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
        raise ValueError("retrieval-fusion-invalid")
    coverage = receipt["coverage"]
    terms = set(query_terms(query))
    anchors = {match[0] for match in _literal_spans(normalize(query))}
    if type(coverage) is not list or len(coverage) != len(expected):
        raise ValueError("retrieval-coverage-invalid")
    for item, ranked in zip(coverage, expected, strict=True):
        if (
            type(item) is not dict
            or set(item) != {"identity", "terms"}
            or item["identity"] != ranked["identity"]
            or type(item["terms"]) is not list
            or not item["terms"]
            or any(type(term) is not str for term in item["terms"])
            or item["terms"] != sorted(set(item["terms"]))
            or not set(item["terms"]) <= terms
            or (anchors and not anchors.intersection(item["terms"]))
        ):
            raise ValueError("retrieval-coverage-invalid")
