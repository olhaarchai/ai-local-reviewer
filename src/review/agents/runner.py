"""AnalystRunner — runs one analyst end-to-end with transparent diff chunking.

Public API: `build_runner(agent_key, state, thread_id).run()` returns the
LangGraph payload dict for `analyst_node`.

Chunking is internal — the caller never knows whether the diff fit in one
LLM call or was split into seven. Splitting is controlled by two env vars:
  - MAX_DIFF_CHARS_PER_CALL — char budget per call (default 40000)
  - MAX_CHUNKS_PER_AGENT    — cap on chunk count (-1=unlimited, 0=disable, >0=cap)

The chunking factory pattern is in place so future runner variants
(parallel, batched-by-category, cloud-only) can plug in here without
touching the graph or node code.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from src.core.progress import get_or_create_run_dir
from src.review.agents.analyst import ANALYSTS, AnalystConfig, build_analyst
from src.review.prompts import compose_analyst_system
from src.review.state import ReviewerState
from src.review.utils import (
    chunk_diff_by_size,
    format_diff_for_llm,
    message_repr,
    state_get,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkResult:
    """Output of one LLM call inside a chunked analyst run."""

    idx: int
    of: int
    chunk_chars: int
    comments: list
    raw_response: str
    last_message: AIMessage | None
    duration_s: float
    structured_ok: bool
    messages: list


def _parse_json_response(content: str, node: str) -> list:
    clean = content.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(clean)
    if isinstance(parsed, dict) and "comments" in parsed:
        parsed = parsed.get("comments", [])
    elif isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Unexpected JSON type: {type(parsed)}")
    logger.info("[%s] Parsed %d structured comments", node, len(parsed))
    return parsed


def _extract_agent_result(
    result: Any, node: str
) -> tuple[list, AIMessage | None, str, list]:
    if not isinstance(result, dict):
        return ([], None, "", [])

    messages = result.get("messages") or []
    last_message = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_message = msg
            break
    if last_message is None:
        return ([], None, "", messages)

    raw_content = getattr(last_message, "content", "")
    structured = result.get("structured_response")
    if structured is not None:
        if isinstance(structured, dict):
            comments = structured.get("comments", [])
            return (comments or [], last_message, raw_content, messages)
        if hasattr(structured, "comments"):
            return (
                list(getattr(structured, "comments") or []),
                last_message,
                raw_content,
                messages,
            )
    if raw_content:
        try:
            comments = _parse_json_response(raw_content, node)
            return (comments, last_message, raw_content, messages)
        except Exception as exc:
            logger.error("[%s] JSON parse failed: %s", node, exc)
    return ([], last_message, raw_content, messages)


class AnalystRunner:
    """Run one analyst end-to-end. Splits oversized diffs transparently."""

    def __init__(
        self, cfg: AnalystConfig, state: ReviewerState, thread_id: str
    ) -> None:
        from src.core.config import settings

        self.cfg = cfg
        self.state = state
        self.thread_id = thread_id
        self.label = cfg.log_label
        self._settings = settings
        self.provider = settings.type_agents or "local"
        self.model_name = getattr(settings, cfg.model_setting, None)
        self.num_ctx = (
            getattr(settings, cfg.num_ctx_setting, None)
            if self.provider == "local"
            else None
        )

    async def run(self) -> dict:
        logger.info(
            "[%s|%s] Starting with model=%s",
            self.label,
            self.provider,
            self.model_name,
        )
        started_at = time.perf_counter()

        system_content, diff_for_llm, diff_format, raw_diff = self._compose()
        chunks = self._chunks(diff_for_llm)
        n_chunks = len(chunks)
        if n_chunks > 1:
            sizes = " + ".join(str(len(c)) for c in chunks)
            logger.info(
                "[%s|%s] split: %d chunks (%s chars)",
                self.label,
                self.provider,
                n_chunks,
                sizes,
            )

        results: list[ChunkResult] = []
        for idx, chunk in enumerate(chunks, 1):
            results.append(
                await self._call_one(system_content, chunk, idx, n_chunks, diff_format)
            )

        total_duration = time.perf_counter() - started_at
        return self._merge(
            results,
            system_content,
            diff_for_llm,
            raw_diff,
            diff_format,
            n_chunks,
            total_duration,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _compose(self) -> tuple[str, str, str, str]:
        """Build the system prompt + LLM-formatted diff for this run."""
        system_content = compose_analyst_system(self.cfg.prompt_dir, self.state)
        guidelines = state_get(self.state, "guidelines", [])
        if guidelines:
            logger.info("[%s] Injected %d project rules", self.label, len(guidelines))
        raw_diff = state_get(self.state, "diff", "")
        diff_format = self._settings.diff_format
        if diff_format == "markdown":
            diff_for_llm = format_diff_for_llm(raw_diff)
        else:
            diff_for_llm = raw_diff
        return system_content, diff_for_llm, diff_format, raw_diff

    def _chunks(self, diff_for_llm: str) -> list[str]:
        """Apply size budget + count cap. Always returns ≥1 element."""
        max_chunks = self._settings.max_chunks_per_agent
        if max_chunks == 0:
            return [diff_for_llm]

        max_chars = self._settings.max_diff_chars_per_call
        if self._settings.diff_format != "markdown" or max_chars <= 0:
            # Chunking only works on markdown-formatted diffs (file-block split).
            # On raw unified diff we can't split safely without breaking @@ headers.
            return [diff_for_llm]

        chunks = chunk_diff_by_size(diff_for_llm, max_chars)
        if not chunks:
            return [diff_for_llm]

        if max_chunks < 0 or len(chunks) <= max_chunks:
            return chunks

        kept, skipped = chunks[:max_chunks], chunks[max_chunks:]
        skipped_chars = sum(len(c) for c in skipped)
        logger.warning(
            "[%s|%s] capped at %d/%d chunks — %d chars (%d chunks) NOT reviewed",
            self.label,
            self.provider,
            max_chunks,
            len(chunks),
            skipped_chars,
            len(skipped),
        )
        self._dump_skipped(skipped, total=len(chunks))
        return kept

    def _format_user_message(self, chunk: str, diff_format: str) -> str:
        if diff_format == "markdown":
            return f"CHANGED CODE (added lines only):\n\n{chunk}"
        return f"DIFF:\n{chunk}"

    async def _call_one(
        self,
        system_content: str,
        chunk: str,
        idx: int,
        total: int,
        diff_format: str,
    ) -> ChunkResult:
        """Invoke the LLM once for one chunk; collect parsed result + raw text."""
        user_message = self._format_user_message(chunk, diff_format)
        suffix = "" if total <= 1 else f"-chunk{idx}"
        self._dump_prompt(system_content, user_message, suffix)

        est_tokens = (len(system_content) + len(user_message)) // 4
        logger.info(
            "[%s|%s] %sSending to LLM: fmt=%s sys_chars=%d user_chars=%d "
            "~tokens=%d num_ctx=%s",
            self.label,
            self.provider,
            f"chunk {idx}/{total}: " if total > 1 else "",
            diff_format,
            len(system_content),
            len(user_message),
            est_tokens,
            self.num_ctx,
        )

        agent = build_analyst(self.cfg, system_content)
        started = time.perf_counter()
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
        )
        duration = time.perf_counter() - started

        comments, last_message, raw_response, messages = _extract_agent_result(
            result, self.label
        )
        structured_ok = (
            bool(result.get("structured_response"))
            if isinstance(result, dict)
            else False
        )

        if total > 1:
            logger.info(
                "[%s|%s] chunk %d/%d done in %.2fs, comments=%d, raw_chars=%d",
                self.label,
                self.provider,
                idx,
                total,
                duration,
                len(comments),
                len(raw_response or ""),
            )

        return ChunkResult(
            idx=idx,
            of=total,
            chunk_chars=len(chunk),
            comments=comments,
            raw_response=raw_response or "",
            last_message=last_message,
            duration_s=duration,
            structured_ok=structured_ok,
            messages=messages,
        )

    def _dump_prompt(self, system_content: str, user_message: str, suffix: str) -> None:
        """Write the exact LLM payload to <agent>-prompt[suffix].txt."""
        try:
            run_dir = get_or_create_run_dir(self.thread_id)
            iteration = int(state_get(self.state, "iterations", 0) or 0)
            retry_suffix = "" if iteration == 0 else f"-retry{iteration}"
            prompt_path = (
                run_dir / f"{self.cfg.agent_key}-prompt{suffix}{retry_suffix}.txt"
            )
            prompt_path.write_text(
                f"=== SYSTEM ===\n{system_content}\n\n=== USER ===\n{user_message}\n",
                encoding="utf-8",
            )
            logger.info(
                "[%s|%s] Prompt dumped to %s",
                self.label,
                self.provider,
                prompt_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Failed to dump prompt: %s", self.label, exc)

    def _dump_skipped(self, skipped: list[str], total: int) -> None:
        """Persist chunks the cap dropped, for human inspection."""
        try:
            run_dir: Path = get_or_create_run_dir(self.thread_id)
            path = run_dir / f"{self.cfg.agent_key}-skipped-chunks.txt"
            blocks = []
            offset = total - len(skipped)
            for i, chunk in enumerate(skipped, 1):
                idx = offset + i
                blocks.append(
                    f"=== SKIPPED CHUNK {idx} of {total} ({len(chunk)} chars) ===\n\n"
                    f"{chunk.rstrip()}\n"
                )
            path.write_text("\n".join(blocks), encoding="utf-8")
            logger.info(
                "[%s|%s] Skipped chunks dumped to %s",
                self.label,
                self.provider,
                path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Failed to dump skipped chunks: %s", self.label, exc)

    def _merge(
        self,
        results: list[ChunkResult],
        system_content: str,
        diff_for_llm: str,
        raw_diff: str,
        diff_format: str,
        n_chunks: int,
        total_duration: float,
    ) -> dict:
        """Combine per-chunk results into a single LangGraph payload."""
        comments: list = []
        raw_parts: list[str] = []
        all_messages: list = []
        last_message: AIMessage | None = None
        all_structured = bool(results) and all(r.structured_ok for r in results)

        for r in results:
            comments.extend(r.comments)
            if r.raw_response:
                if n_chunks > 1:
                    raw_parts.append(f"--- chunk {r.idx} ---\n{r.raw_response}")
                else:
                    raw_parts.append(r.raw_response)
            if r.last_message is not None:
                last_message = r.last_message
            all_messages.extend(r.messages)

        joined_raw = "\n\n".join(raw_parts)
        chunk_traces = [
            {
                "idx": r.idx,
                "of": r.of,
                "chars": r.chunk_chars,
                "duration_s": r.duration_s,
                "comments": len(r.comments),
                "raw_chars": len(r.raw_response),
                "structured_ok": r.structured_ok,
            }
            for r in results
        ]

        # Compute skipped count by inspecting whether _chunks dropped any.
        max_chunks = self._settings.max_chunks_per_agent
        max_chars = self._settings.max_diff_chars_per_call
        skipped = 0
        if max_chunks > 0 and diff_format == "markdown" and max_chars > 0:
            full = chunk_diff_by_size(diff_for_llm, max_chars)
            if len(full) > max_chunks:
                skipped = len(full) - max_chunks

        logger.info(
            "[%s|%s] model=%s fmt=%s sys_chars=%d diff_chars=%d raw_chars=%d "
            "structured=%s comments=%d chunks=%d%s took=%.2fs",
            self.label,
            self.provider,
            self.model_name,
            diff_format,
            len(system_content),
            len(diff_for_llm),
            len(joined_raw),
            all_structured,
            len(comments),
            n_chunks,
            f" skipped={skipped}" if skipped else "",
            total_duration,
        )

        trace: dict[str, Any] = {
            "agent": self.cfg.agent_key,
            "provider": self.provider,
            "model": self.model_name,
            "temperature": self.cfg.temperature,
            "num_ctx": self.num_ctx,
            "inputs": {
                "system_content": system_content,
                "system_chars": len(system_content),
                "diff_format": diff_format,
                "diff": diff_for_llm,
                "diff_chars": len(diff_for_llm),
                "raw_diff_chars": len(raw_diff),
            },
            "outputs": {
                "messages": [message_repr(m) for m in all_messages],
                "raw_text": joined_raw,
                "raw_chars": len(joined_raw),
                "structured_ok": all_structured,
                "comments_count": len(comments),
            },
            "duration_s": total_duration,
            "chunks": chunk_traces,
        }
        if skipped:
            trace["skipped_chunks"] = skipped

        payload: dict[str, Any] = {"analyst_traces": [trace]}
        if last_message is not None:
            payload["messages"] = [last_message]
        if joined_raw:
            payload["raw_responses"] = [joined_raw]
        if comments:
            payload["comments"] = comments
        payload["timings"] = [{self.label: total_duration}]

        metrics: dict[str, Any] = {
            "agent": self.cfg.agent_key,
            "comments": len(comments),
            "raw_chars": len(joined_raw),
            "chunks": n_chunks,
        }
        if skipped:
            metrics["skipped"] = skipped
        payload["_progress_metrics"] = metrics
        return payload


def build_runner(agent_key: str, state: ReviewerState, thread_id: str) -> AnalystRunner:
    """Factory — pick a runner variant. Currently single class.

    Extension point: a future `ParallelChunkRunner` or `BatchByCategoryRunner`
    can be selected here based on settings without touching graph or nodes.
    """
    return AnalystRunner(ANALYSTS[agent_key], state, thread_id)
