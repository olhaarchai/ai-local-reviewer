"""Prompt loading + composition for analyst agents.

All prompt files live under /prompts. Static fragments are concatenated;
runtime templates are formatted via str.format — missing keys fall back to
empty strings. Static files are cached after first read.

Layout:
    prompts/
      agents/<dir>/persona.md     required per analyst
      agents/<dir>/focus.md       optional (e.g., OWASP categories)
      agents/<dir>/schema.md      required per analyst
      fragments/*.md              shared anti-pattern blocks
      runtime/*.tmpl              str.format templates populated from state
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.review.utils import state_get

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_STATIC_CACHE: dict[str, str] = {}


def load(relpath: str, *, silent: bool = False) -> str:
    """Read a prompt file under /prompts. Returns "" if missing. Cached.

    Pass `silent=True` for optional files (e.g. per-agent focus.md) to
    skip the "file not found" warning.
    """
    if relpath in _STATIC_CACHE:
        return _STATIC_CACHE[relpath]
    path = _PROMPTS_DIR / relpath
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not silent:
            logger.warning("[prompts] file not found: %s", path)
        text = ""
    _STATIC_CACHE[relpath] = text
    return text


def render(tmpl_relpath: str, **kwargs: Any) -> str:
    """Load a .tmpl and format with kwargs. Empty on missing file or key error."""
    tmpl = load(tmpl_relpath)
    if not tmpl:
        return ""
    try:
        return tmpl.format(**kwargs)
    except KeyError as exc:
        logger.warning("[prompts] missing key %s in template %s", exc, tmpl_relpath)
        return ""


def compose_analyst_system(agent_dir: str, state: Any) -> str:
    """Build the full system prompt for an analyst.

    Order:
      1. runtime/stack-context.tmpl    — if stack_context present
      2. agents/<dir>/persona.md
      3. agents/<dir>/focus.md         — if file exists
      4. agents/<dir>/schema.md
      5. fragments/line-precision.md
      6. fragments/example-bad.md
      7. fragments/rule-applicability.md
      8. runtime/project-rules.tmpl    — if guidelines present
      9. runtime/lint-findings.tmpl    — if lint_findings present
      10. runtime/critic-feedback.tmpl — if critic_feedback present
    """
    parts: list[str] = []

    stack_context = state_get(state, "stack_context", "") or ""
    if stack_context:
        parts.append(render("runtime/stack-context.tmpl", stack_context=stack_context))

    parts.append(load(f"agents/{agent_dir}/persona.md"))
    focus = load(f"agents/{agent_dir}/focus.md", silent=True)
    if focus:
        parts.append(focus)
    parts.append(load(f"agents/{agent_dir}/schema.md"))

    parts.append(load("fragments/line-precision.md"))
    parts.append(load("fragments/example-bad.md"))
    parts.append(load("fragments/rule-applicability.md"))

    guidelines = state_get(state, "guidelines", []) or []
    if guidelines:
        rules_text = "\n".join(
            f"- {g.text if hasattr(g, 'text') else g}" for g in guidelines
        )
        parts.append(render("runtime/project-rules.tmpl", rules_text=rules_text))

    lint_findings = state_get(state, "lint_findings", []) or []
    if lint_findings:
        findings = "\n".join(f"- {f}" for f in lint_findings)
        parts.append(render("runtime/lint-findings.tmpl", findings=findings))

    critic_feedback = state_get(state, "critic_feedback", None)
    if critic_feedback:
        parts.append(render("runtime/critic-feedback.tmpl", feedback=critic_feedback))

    return "\n".join(p.strip() for p in parts if p and p.strip()) + "\n"
