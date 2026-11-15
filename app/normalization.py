# -*- coding: utf-8 -*-
"""Query text rules.

Two deliberately separate operations:

1. **Normalization** — mechanical only: Unicode NFKC, ASCII case folding and
   whitespace collapsing. The original wording is always kept alongside, so no
   information is destroyed.
2. **Synonym merging** — applied *only* through the page-maintained explicit
   mapping table. There is no stemming, no fuzzy match, no language model. A
   query that has no mapping entry is its own canonical form.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata

_WS_RE = re.compile(r"\s+")


def normalize_query(raw: str | None) -> str:
    """Canonical mechanical form: NFKC + case fold + collapsed whitespace."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = text.casefold()
    text = _WS_RE.sub(" ", text).strip()
    return text


def load_mapping(conn: sqlite3.Connection, version_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT source_term, target_term FROM mapping_entries WHERE version_id = ?",
        (version_id,),
    ).fetchall()
    return {r["source_term"]: r["target_term"] for r in rows}


def canonicalize(query_norm: str, mapping: dict[str, str]) -> str:
    """Apply the explicit mapping.

    Rule order (both consult the same maintained table, nothing fuzzy):
      1. Exact whole-query match wins.
      2. Otherwise each whitespace token that is an exact mapping key is
         replaced; unmapped tokens pass through unchanged.
    """
    if not query_norm:
        return ""
    if query_norm in mapping:
        return mapping[query_norm]
    tokens = query_norm.split(" ")
    return " ".join(mapping.get(tok, tok) for tok in tokens)


def load_word_class_rules(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT keyword, word_class FROM word_class_rules ORDER BY rule_order, keyword"
    ).fetchall()
    return [(r["keyword"], r["word_class"]) for r in rows]


def classify(query_norm: str, rules: list[tuple[str, str]]) -> str:
    """First explicit substring rule wins; otherwise 'other'."""
    for keyword, word_class in rules:
        if keyword and keyword in query_norm:
            return word_class
    return "other"
