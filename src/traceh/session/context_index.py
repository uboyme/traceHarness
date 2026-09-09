"""Frozen, rebuildable FTS rows owned by the production EventStore worker."""

from __future__ import annotations

import json
from dataclasses import dataclass

from traceh.api.json_types import canonical_json, fingerprint

CREATE_MANIFEST = """CREATE TABLE context_index_manifest (
    corpus_key TEXT PRIMARY KEY NOT NULL,
    manifest_json TEXT NOT NULL
) WITHOUT ROWID"""
CREATE_ITEMS = """CREATE TABLE context_index_items (
    rowid INTEGER PRIMARY KEY,
    corpus_key TEXT NOT NULL,
    item_json TEXT NOT NULL
)"""
CREATE_FTS = "CREATE VIRTUAL TABLE context_fts USING fts5(terms, tokenize=unicode61)"
INDEX_DDL = {
    "context_index_manifest": CREATE_MANIFEST,
    "context_index_items": CREATE_ITEMS,
    "context_fts": CREATE_FTS,
    "context_fts_config": "CREATE TABLE 'context_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID",
    "context_fts_content": "CREATE TABLE 'context_fts_content'(id INTEGER PRIMARY KEY, c0)",
    "context_fts_data": "CREATE TABLE 'context_fts_data'(id INTEGER PRIMARY KEY, block BLOB)",
    "context_fts_docsize": "CREATE TABLE 'context_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)",
    "context_fts_idx": (
        "CREATE TABLE 'context_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID"
    ),
}


@dataclass(frozen=True, slots=True)
class ContextCorpus:
    """Canonical derived input; never a mutable cache or connection capability."""

    manifest_json: str
    items_json: tuple[str, ...]

    @property
    def key(self):
        return json.loads(self.manifest_json)["corpus_key"]

    @classmethod
    def build(cls, *, scope, catalog_digest, source_heads, config_digest, items):
        identity = {
            "scope": scope,
            "catalog_digest": catalog_digest,
            "source_heads": source_heads,
            "tokenizer": "traceh-lexical-v3",
            "ranker": "eligible-bm25-v2",
            "config_digest": config_digest,
        }
        rows = tuple(canonical_json(item) for item in items)
        manifest = {
            **identity,
            "corpus_key": fingerprint(identity),
            "corpus_digest": fingerprint(items),
            "item_count": len(rows),
            "item_bytes": sum(len(row.encode("utf-8")) for row in rows),
        }
        return cls(canonical_json(manifest), rows)


def source_heads_match(connection, corpus):
    for ref in json.loads(corpus.manifest_json)["source_heads"]:
        row = connection.execute(
            "SELECT head_seq FROM streams WHERE stream_id=?", (ref["stream_id"],)
        ).fetchone()
        if (row[0] if row else 0) != ref["head_seq"]:
            return False
        if ref["head_seq"]:
            raw = connection.execute(
                "SELECT envelope_json FROM events WHERE stream_id=? AND seq=?",
                (ref["stream_id"], ref["head_seq"]),
            ).fetchone()
            if raw is None:
                return False
            event = json.loads(raw[0])
            if (
                event["event_id"] != ref["head_event_id"]
                or fingerprint(event) != ref["head_digest"]
            ):
                return False
    return True


def matches(connection, corpus, *, require_current_sources=True):
    manifest = connection.execute(
        "SELECT manifest_json FROM context_index_manifest WHERE corpus_key=?", (corpus.key,)
    ).fetchone()
    if manifest != (corpus.manifest_json,) or (
        require_current_sources and not source_heads_match(connection, corpus)
    ):
        return False
    rows = connection.execute(
        "SELECT i.item_json,f.terms FROM context_index_items i "
        "LEFT JOIN context_fts f ON f.rowid=i.rowid "
        "WHERE i.corpus_key=? ORDER BY i.rowid",
        (corpus.key,),
    ).fetchall()
    return rows == [(row, json.loads(row)["terms"]) for row in corpus.items_json]


def rebuild(connection, corpus):
    if not source_heads_match(connection, corpus):
        raise ValueError("context-index-source-changed")
    # A damaged derived items table can leave FTS rowids behind. Remove those
    # known orphan rows before SQLite allocates new item rowids in this transaction.
    connection.execute(
        "DELETE FROM context_fts WHERE rowid NOT IN (SELECT rowid FROM context_index_items)"
    )
    connection.execute(
        "DELETE FROM context_fts WHERE rowid IN "
        "(SELECT rowid FROM context_index_items WHERE corpus_key=?)",
        (corpus.key,),
    )
    connection.execute("DELETE FROM context_index_items WHERE corpus_key=?", (corpus.key,))
    for raw in corpus.items_json:
        cursor = connection.execute(
            "INSERT INTO context_index_items(corpus_key,item_json) VALUES (?,?)", (corpus.key, raw)
        )
        connection.execute(
            "INSERT INTO context_fts(rowid,terms) VALUES (?,?)",
            (cursor.lastrowid, json.loads(raw)["terms"]),
        )
    connection.execute(
        "INSERT INTO context_index_manifest(corpus_key,manifest_json) VALUES (?,?) "
        "ON CONFLICT(corpus_key) DO UPDATE SET manifest_json=excluded.manifest_json",
        (corpus.key, corpus.manifest_json),
    )


def query(connection, corpus, terms):
    if not matches(connection, corpus):
        return None
    if not terms:
        return ()
    # Only encoded host tokens can become SQL FTS grammar; raw queries never do.
    expression = " OR ".join('"t' + term.encode("utf-8").hex() + '"' for term in sorted(set(terms)))
    rows = connection.execute(
        "SELECT i.item_json FROM context_fts JOIN context_index_items i "
        "ON i.rowid=context_fts.rowid WHERE context_fts MATCH ? AND i.corpus_key=? "
        "ORDER BY i.rowid",
        (expression, corpus.key),
    ).fetchall()
    # Rows are verified against the complete, already eligible canonical corpus.
    return tuple(json.loads(row[0])["identity"] for row in rows)
