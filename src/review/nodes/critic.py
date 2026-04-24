"""Critic node — deterministic pruner of analyst comments.

Applies G1-G4 guards + rule-ID membership. Does not call an LLM.

G1: line must be in the diff's + set for that path.
G2: path must appear in the diff.
G3: any backtick-quoted identifier in the body must appear in the file's added lines.
G4: if body starts with [RULEID] and the rule is in the corpus, its category
    must match the file's detected stack (unless the rule is always-include).
UNKNOWN_RULE: [RULEID] not found anywhere (index + retrieved guidelines).
GUIDELINE_MISS: [RULEID] exists in index but wasn't retrieved for this PR.
FORMAT: empty path/line/body.
"""

import logging
import re
from typing import Any

from langgraph.types import Overwrite

from src.integrations.retriever import _ALWAYS_INCLUDE, _classify_file
from src.integrations.sparse_index import SPARSE_INDEX
from src.review.state import ReviewerState
from src.review.utils import (
    build_added_content_map,
    build_added_line_map,
    extract_comment_fields,
    state_get,
)

logger = logging.getLogger(__name__)

_RULE_ID_PATTERN = re.compile(
    r"^\[(?P<rule>[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)?(?:\d+)?)\]"
)
_GUIDELINE_RULE_PATTERN = re.compile(r"\[(?P<rule>[A-Za-z]+[0-9]{3,})\]")
_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def _extract_rule_ids(guidelines: list) -> set[str]:
    rule_ids: set[str] = set()
    for g in guidelines:
        if hasattr(g, "id"):
            if g.id != "UNKNOWN":
                rule_ids.add(g.id)
        else:
            for match in _GUIDELINE_RULE_PATTERN.finditer(str(g) or ""):
                rule_ids.add(match.group("rule"))
    return rule_ids


def _reject(
    comment: Any,
    reason_code: str,
    detail: str,
    rejections: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    counts[reason_code] = counts.get(reason_code, 0) + 1
    rejections.append({"comment": comment, "reason": f"{reason_code}: {detail}"})


def critic_node(state: ReviewerState) -> dict:
    comments = state_get(state, "comments", []) or []
    guidelines = state_get(state, "guidelines", []) or []
    raw_responses = state_get(state, "raw_responses", []) or []
    diff = state_get(state, "diff", "")
    iterations = int(state_get(state, "iterations", 0) or 0) + 1

    # Zero-comments fallback: if analyst returned raw text but nothing parsed,
    # ask for one retry with a formatting hint. Otherwise accept "no findings".
    if not comments:
        issues: list[dict[str, Any]] = []
        critic_feedback: str | None = None
        if raw_responses:
            issues.append(
                {
                    "path": "unknown",
                    "line": 0,
                    "rule_id": "FORMAT",
                    "message": "Analyst output is not valid JSON. Preserve content and fix formatting.",
                }
            )
            critic_feedback = (
                "Analyst output is not valid JSON. Preserve content and fix formatting."
            )
        is_valid = len(issues) == 0
        logger.info(
            "[critic] Empty comments — %s with %d issue(s)",
            "passed" if is_valid else "requesting retry",
            len(issues),
        )
        return {
            "is_valid": is_valid,
            "critic_feedback": critic_feedback,
            "critic_issues": issues,
            "critic_counts": {"FORMAT": 1} if issues else {},
            "iterations": iterations,
        }

    added_lines = build_added_line_map(diff)
    added_content = build_added_content_map(diff)
    guideline_rules = _extract_rule_ids(guidelines)

    survivors: list[Any] = []
    rejections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for comment in comments:
        path, line, body = extract_comment_fields(comment)
        body_stripped = body.strip()

        if not path or line <= 0 or not body_stripped:
            _reject(
                comment,
                "FORMAT",
                f"missing path/line/body ({path}:{line})",
                rejections,
                counts,
            )
            continue

        if path not in added_lines:
            _reject(comment, "G2", f"path '{path}' not in diff", rejections, counts)
            continue

        if line not in added_lines[path]:
            _reject(
                comment,
                "G1",
                f"line {line} not in '+'-set for '{path}'",
                rejections,
                counts,
            )
            continue

        identifiers = _BACKTICK_IDENT_RE.findall(body_stripped)
        if identifiers:
            file_added = added_content.get(path, "")
            if not any(ident in file_added for ident in identifiers):
                _reject(
                    comment,
                    "G3",
                    f"none of {identifiers} found in added lines of '{path}'",
                    rejections,
                    counts,
                )
                continue

        rule_match = _RULE_ID_PATTERN.match(body_stripped)
        if rule_match:
            rule_id = rule_match.group("rule")
            rule_meta = SPARSE_INDEX.lookup_by_id(rule_id)
            expected_cat = _classify_file(path)
            if rule_meta is None:
                if guideline_rules and rule_id not in guideline_rules:
                    _reject(
                        comment,
                        "UNKNOWN_RULE",
                        f"rule [{rule_id}] unknown to index and guidelines",
                        rejections,
                        counts,
                    )
                    continue
            else:
                rule_cat = rule_meta.get("category")
                if (
                    expected_cat is not None
                    and rule_cat != expected_cat
                    and rule_cat not in _ALWAYS_INCLUDE
                ):
                    _reject(
                        comment,
                        "G4",
                        f"rule [{rule_id}] category '{rule_cat}' != file cat '{expected_cat}'",
                        rejections,
                        counts,
                    )
                    continue
                if guideline_rules and rule_id not in guideline_rules:
                    _reject(
                        comment,
                        "GUIDELINE_MISS",
                        f"rule [{rule_id}] not in retrieved guidelines",
                        rejections,
                        counts,
                    )
                    continue

        survivors.append(comment)

    count_str = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "none"
    logger.info(
        "[critic] in=%d survivors=%d rejected=%d counts={%s}",
        len(comments),
        len(survivors),
        len(rejections),
        count_str,
    )

    return {
        "comments": Overwrite(value=survivors),
        "is_valid": True,
        "critic_feedback": None,
        "critic_issues": [
            {"comment": r["comment"], "reason": r["reason"]} for r in rejections
        ],
        "critic_counts": counts,
        "iterations": iterations,
    }


def retry_node(state: ReviewerState) -> dict:
    iterations = state_get(state, "iterations", 0)
    logger.info("[critic] Retry requested (iteration %s)", iterations)
    return {
        "comments": Overwrite(value=[]),
        "messages": Overwrite(value=[]),
        "raw_responses": Overwrite(value=[]),
        "critic_issues": [],
        "route": None,
    }
