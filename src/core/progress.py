"""Per-PR progress trail under output/processes/<thread>-<ts>/progress.log.

Purpose: when a node hangs, you can tail the file and see which stage was
last entered and which never exited. Zero changes to node bodies — wiring
is a single wrapper in graph.py that logs ENTER/EXIT around each node call.

Run directories are keyed by thread_id (the webhook sets this to
`<repo>#<pr_number>`) so retries of the same PR in the same process append
to the same progress.log — which is what you want when debugging a retry.

The writer is intentionally best-effort: any IO error is logged and
swallowed so a broken progress file can never take down a review.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

_run_dirs: dict[str, Path] = {}
_lock = Lock()


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "run"


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _append(run_dir: Path, line: str) -> None:
    try:
        with (run_dir / "progress.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[progress] write failed: %s", exc)


def get_or_create_run_dir(thread_id: str, base: str = "output/processes") -> Path:
    with _lock:
        cached = _run_dirs.get(thread_id)
        if cached is not None:
            return cached
        from src.core.review_log import model_slug

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        run_dir = Path(base) / f"{_slug(thread_id)}-{model_slug()}-{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _run_dirs[thread_id] = run_dir
        _append(run_dir, f"{_now()}  START  thread_id={thread_id}")
        return run_dir


def reset_run(thread_id: str) -> None:
    with _lock:
        run_dir = _run_dirs.pop(thread_id, None)
    if run_dir is not None:
        _append(run_dir, f"{_now()}  END    thread_id={thread_id}")


def log_enter(thread_id: str, name: str) -> None:
    run_dir = get_or_create_run_dir(thread_id)
    _append(run_dir, f"{_now()}  ENTER  {name}")


def log_exit(thread_id: str, name: str, duration_s: float) -> None:
    run_dir = _run_dirs.get(thread_id)
    if run_dir is None:
        return
    _append(run_dir, f"{_now()}  EXIT   {name:<16} ({duration_s:.2f}s)")


def log_error(thread_id: str, name: str, exc: BaseException) -> None:
    run_dir = _run_dirs.get(thread_id)
    if run_dir is None:
        return
    _append(run_dir, f"{_now()}  ERROR  {name}: {type(exc).__name__}: {exc}")


def log_pause(thread_id: str, name: str, reason: str) -> None:
    """Control-flow pause (e.g. LangGraph interrupt for HITL) — not an error."""
    run_dir = _run_dirs.get(thread_id)
    if run_dir is None:
        return
    _append(run_dir, f"{_now()}  PAUSE  {name:<16} ({reason})")
