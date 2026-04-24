"""Single analyst node — dispatches to any entry in ANALYSTS registry.

graph.py wires one `partial(analyst_node, key)` per enabled agent; no per-agent
node function needed. Adding an analyst = adding an ANALYSTS entry + prompts.
"""

import json
import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from src.core.progress import get_or_create_run_dir
from src.review.agents.analyst import ANALYSTS, build_analyst
from src.review.prompts import compose_analyst_system
from src.review.state import ReviewerState
from src.review.utils import format_diff_for_llm, message_repr, state_get

logger = logging.getLogger(__name__)


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


def _extract_agent_result(result: Any, node: str) -> tuple[list, AIMessage | None, str]:
    if not isinstance(result, dict):
        return ([], None, "")

    messages = result.get("messages") or []
    last_message = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_message = msg
            break
    if last_message is None:
        return ([], None, "")

    raw_content = getattr(last_message, "content", "")
    structured = result.get("structured_response")
    if structured is not None:
        if isinstance(structured, dict):
            comments = structured.get("comments", [])
            return (comments or [], last_message, raw_content)
        if hasattr(structured, "comments"):
            return (
                list(getattr(structured, "comments") or []),
                last_message,
                raw_content,
            )
    if raw_content:
        try:
            comments = _parse_json_response(raw_content, node)
            return (comments, last_message, raw_content)
        except Exception as exc:
            logger.error("[%s] JSON parse failed: %s", node, exc)
    return ([], last_message, raw_content)


async def analyst_node(
    agent_key: str, state: ReviewerState, config: dict | None = None
) -> dict:
    """Single analyst runner. Dispatches on ANALYSTS[agent_key]."""
    cfg = ANALYSTS[agent_key]
    label = cfg.log_label
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id") or "default"

    from src.core.config import settings

    provider = settings.type_agents or "local"
    model_name = getattr(settings, cfg.model_setting, None)
    num_ctx = (
        getattr(settings, cfg.num_ctx_setting, None) if provider == "local" else None
    )
    logger.info("[%s|%s] Starting with model=%s", label, provider, model_name)
    started_at = time.perf_counter()

    system_content = compose_analyst_system(cfg.prompt_dir, state)
    guidelines = state_get(state, "guidelines", [])
    if guidelines:
        logger.info("[%s] Injected %d project rules", label, len(guidelines))

    diff = state_get(state, "diff", "")
    diff_format = settings.diff_format
    if diff_format == "markdown":
        diff_for_llm = format_diff_for_llm(diff)
        user_message = f"CHANGED CODE (added lines only):\n\n{diff_for_llm}"
    else:
        diff_for_llm = diff
        user_message = f"DIFF:\n{diff}"
    # Rough token estimate: 1 token ≈ 3.5 chars for English code/prose.
    est_tokens = (len(system_content) + len(user_message)) // 4
    logger.info(
        "[%s|%s] Sending to LLM: fmt=%s sys_chars=%d user_chars=%d total_chars=%d ~tokens=%d num_ctx=%s",
        label,
        provider,
        diff_format,
        len(system_content),
        len(user_message),
        len(system_content) + len(user_message),
        est_tokens,
        num_ctx,
    )
    logger.debug("[%s] SYSTEM PROMPT:\n%s", label, system_content)
    logger.debug("[%s] USER MESSAGE:\n%s", label, user_message)

    # Dump the exact prompt payload next to progress.log so it can be pasted
    # verbatim into `ollama run` for offline debugging. One file per call —
    # the iteration counter comes from the critic (retries overwrite previous).
    try:
        run_dir = get_or_create_run_dir(thread_id)
        iteration = int(state_get(state, "iterations", 0) or 0)
        suffix = "" if iteration == 0 else f"-retry{iteration}"
        prompt_path = run_dir / f"{agent_key}-prompt{suffix}.txt"
        prompt_path.write_text(
            f"=== SYSTEM ===\n{system_content}\n\n=== USER ===\n{user_message}\n",
            encoding="utf-8",
        )
        logger.info("[%s|%s] Prompt dumped to %s", label, provider, prompt_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Failed to dump prompt: %s", label, exc)

    agent = build_analyst(cfg, system_content)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
    )

    comments, last_message, raw_response = _extract_agent_result(result, label)
    duration = time.perf_counter() - started_at
    structured_ok = (
        bool(result.get("structured_response")) if isinstance(result, dict) else False
    )

    logger.info(
        "[%s|%s] model=%s fmt=%s sys_chars=%d diff_chars=%d raw_chars=%d structured=%s comments=%d took=%.2fs",
        label,
        provider,
        model_name,
        diff_format,
        len(system_content),
        len(diff_for_llm),
        len(raw_response or ""),
        structured_ok,
        len(comments),
        duration,
    )
    if last_message:
        logger.debug("[%s] Raw response:\n%s", label, raw_response)

    messages = result.get("messages") if isinstance(result, dict) else None
    trace = {
        "agent": agent_key,
        "provider": provider,
        "model": model_name,
        "temperature": cfg.temperature,
        "num_ctx": num_ctx,
        "inputs": {
            "system_content": system_content,
            "system_chars": len(system_content),
            "diff_format": diff_format,
            "diff": diff_for_llm,
            "diff_chars": len(diff_for_llm),
            "raw_diff_chars": len(diff),
        },
        "outputs": {
            "messages": [message_repr(m) for m in (messages or [])],
            "raw_text": raw_response or "",
            "raw_chars": len(raw_response or ""),
            "structured_ok": structured_ok,
            "comments_count": len(comments),
        },
        "duration_s": duration,
    }

    payload: dict[str, Any] = {"analyst_traces": [trace]}
    if last_message:
        payload["messages"] = [last_message]
    if raw_response:
        payload["raw_responses"] = [raw_response]
    if comments:
        payload["comments"] = comments
    payload["timings"] = [{label: duration}]
    return payload
