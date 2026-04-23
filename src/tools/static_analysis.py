"""Deterministic static analysis — ruff for Python. Used by `linter_node`.

Also exported as a LangChain `@tool` for future agent use. The cardinal rule
(plan doc §7.7): if a static tool can find it, the LLM must not try to.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_RUFF_TIMEOUT_SEC = 10
_ruff_missing_logged = False

# Ruff rule subset that stays correct on diff-fragment content (no full-file
# context). Excludes F401/F811/F821 because those rely on seeing imports and
# top-level decls that a +line reconstruction cannot provide. Keeps the real
# code-smell signal: long lines, bare excepts, mutable defaults, unused vars
# inside a function body, redundant comparisons.
_RUFF_FRAGMENT_SAFE_SELECT = (
    "E501,E701,E702,E703,E711,E712,E713,E714,E721,E722,E731,E741,"
    "F541,F601,F602,F631,F632,F701,F702,F704,F841,"
    "W291,W292,W293,W605,"
    "B006,B007,B015,B018,B020"
)


def _ruff_available() -> bool:
    global _ruff_missing_logged
    if shutil.which("ruff") is None:
        if not _ruff_missing_logged:
            logger.warning("[static_analysis] `ruff` binary not found on PATH")
            _ruff_missing_logged = True
        return False
    return True


def run_ruff_on_file(path: str, content: str) -> list[dict]:
    """Run ruff on in-memory content, returning parsed JSON findings.

    `path` is used only for the temp file's suffix/basename hint; ruff will
    report `filename` pointing at the temp path. Callers should rewrite the
    filename back to `path` when formatting findings.
    """
    if not content.strip():
        return []
    if not _ruff_available():
        return []

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = Path(f.name)
        result = subprocess.run(
            [
                "ruff",
                "check",
                "--output-format=json",
                "--no-fix",
                f"--select={_RUFF_FRAGMENT_SAFE_SELECT}",
                "--isolated",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_SEC,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return []
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.warning("[static_analysis] ruff JSON parse failed: %s", exc)
            return []
    except subprocess.TimeoutExpired:
        logger.warning("[static_analysis] ruff timed out on %s", path)
        return []
    except Exception as exc:
        logger.warning("[static_analysis] ruff failed on %s: %s", path, exc)
        return []
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


@tool
async def run_static_analysis_python(path: str, content: str) -> str:
    """Run ruff on the provided Python file content.

    Returns a JSON string of findings (ruff's native format). Use this to cite
    deterministic, ground-truth lint findings (unused imports, long lines,
    mutable defaults, etc). Do not re-derive these issues with LLM reasoning —
    ruff is authoritative for what it catches.

    Args:
        path: the file's original path (for labelling).
        content: the full text of the Python file.
    """
    findings = run_ruff_on_file(path, content)
    return json.dumps(findings, ensure_ascii=False)
