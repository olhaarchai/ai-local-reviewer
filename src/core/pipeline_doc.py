"""Single source of truth for graph node descriptions.

Used by:
  - src/core/progress.py — appended after each ENTER line in progress.log
  - INSTRUCTION.md — pipeline walkthrough table for the course demo
  - README.md — short pipeline overview

Keeping the map in one place means editing a description in code instantly
updates the audit trail, with no chance of doc drift.
"""

from __future__ import annotations

NODE_DESCRIPTIONS: dict[str, str] = {
    "filter": "strips lockfiles, binaries and noise from the diff",
    "retriever": "pulls project rules from Milvus + BM25 by detected stack",
    "linter": "runs ruff on Python added lines as deterministic pre-findings",
    "security_analyst": "LLM scan for OWASP-class vulnerabilities",
    "style_analyst": "LLM scan for type safety, dead code, framework idioms",
    "critic": "applies G1-G4 guards + rule-ID membership to LLM comments",
    "retry": "clears analyst state for another pass on critic feedback",
    "hitl_gate": "pauses for human approve / retry decision",
    "summarizer": "deterministic executive summary from surviving comments",
}


def describe(node_name: str) -> str:
    """Return the short prose description for a node, or '' if unknown."""
    return NODE_DESCRIPTIONS.get(node_name, "")
