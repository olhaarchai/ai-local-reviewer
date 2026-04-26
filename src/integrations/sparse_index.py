"""In-process BM25 index over `rules/*.md` — complements dense Milvus retrieval.

Milvus 2.3.0 has no native BM25/sparse vectors, so we keep exact-token lookups
in memory. See `.claude/rag-improvements.md` §2.2 (Path A) for rationale.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

_RULES_DIR = Path(__file__).parent.parent.parent / "rules"
_RULE_ID_RE = re.compile(r"^\[([A-Za-z]+\d+)\]")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{3,}")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _parse_rules(path: Path) -> list[str]:
    rules: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            rule = stripped[2:].strip()
            if rule:
                rules.append(rule)
    return rules


class SparseRuleIndex:
    """BM25 over rule bullets with category filtering and O(1) rule_id lookup."""

    def __init__(self, rules: list[dict]) -> None:
        self.rules = rules
        self._by_id: dict[str, dict] = {}
        self._by_category: dict[str, list[int]] = {}
        for idx, rule in enumerate(rules):
            rid = rule.get("rule_id")
            if rid and rid != "UNKNOWN":
                self._by_id[rid] = rule
            self._by_category.setdefault(rule["category"], []).append(idx)
        if rules:
            self._bm25 = BM25Okapi([tokenize(r["text"]) for r in rules])
        else:
            self._bm25 = None

    @classmethod
    def from_rules_dir(cls, rules_dir: Path | None = None) -> "SparseRuleIndex":
        rules_dir = rules_dir or _RULES_DIR
        if not rules_dir.exists():
            logger.warning("[sparse_index] rules_dir missing: %s", rules_dir)
            return cls(rules=[])
        rules: list[dict] = []
        for md in sorted(rules_dir.glob("*.md")):
            category = md.stem
            for text in _parse_rules(md):
                m = _RULE_ID_RE.match(text)
                rule_id = m.group(1) if m else "UNKNOWN"
                rules.append({"text": text, "category": category, "rule_id": rule_id})
        if not rules:
            logger.warning("[sparse_index] no rules parsed from %s", rules_dir)
        else:
            logger.info(
                "[sparse_index] loaded %d rules across %d categories",
                len(rules),
                len({r["category"] for r in rules}),
            )
        return cls(rules=rules)

    def search(
        self,
        query: str,
        k: int,
        category: str | None = None,
    ) -> list[tuple[int, float]]:
        if not self.rules or self._bm25 is None or k <= 0:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        if category is not None:
            indices = self._by_category.get(category, [])
            if not indices:
                return []
            scores = self._bm25.get_scores(tokens)
            pairs = [(i, float(scores[i])) for i in indices]
        else:
            scores = self._bm25.get_scores(tokens)
            pairs = [(i, float(s)) for i, s in enumerate(scores)]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:k]

    def lookup_by_id(self, rule_id: str) -> dict | None:
        if not rule_id:
            return None
        return self._by_id.get(rule_id)

    def get(self, index: int) -> dict:
        return self.rules[index]


SPARSE_INDEX: SparseRuleIndex = SparseRuleIndex.from_rules_dir()
