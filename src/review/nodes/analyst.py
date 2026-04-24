"""Single analyst node — dispatches to any entry in ANALYSTS registry.

graph.py wires one `partial(analyst_node, key)` per enabled agent; no per-agent
node function needed. Adding an analyst = adding an ANALYSTS entry + prompts.
"""

import json
import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from src.review.agents.analyst import ANALYSTS, build_analyst
from src.review.prompts import compose_analyst_system
from src.review.state import ReviewerState
from src.review.utils import message_repr, state_get

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


async def analyst_node(agent_key: str, state: ReviewerState) -> dict:
    """Single analyst runner. Dispatches on ANALYSTS[agent_key]."""
    cfg = ANALYSTS[agent_key]
    label = cfg.log_label

    from src.core.config import settings

    model_name = getattr(settings, cfg.model_setting, None)
    num_ctx = getattr(settings, cfg.num_ctx_setting, None)
    logger.info("[%s] Starting with model=%s", label, model_name)
    started_at = time.perf_counter()

    system_content = compose_analyst_system(cfg.prompt_dir, state)
    guidelines = state_get(state, "guidelines", [])
    if guidelines:
        logger.info("[%s] Injected %d project rules", label, len(guidelines))

    diff = state_get(state, "diff", "")
    agent = build_analyst(cfg, system_content)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=f"DIFF:\n{diff}")]},
    )

    comments, last_message, raw_response = _extract_agent_result(result, label)
    duration = time.perf_counter() - started_at
    structured_ok = (
        bool(result.get("structured_response")) if isinstance(result, dict) else False
    )

    logger.info(
        "[%s] model=%s sys_chars=%d diff_chars=%d raw_chars=%d structured=%s comments=%d took=%.2fs",
        label,
        model_name,
        len(system_content),
        len(diff),
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
        "model": model_name,
        "temperature": cfg.temperature,
        "num_ctx": num_ctx,
        "inputs": {
            "system_content": system_content,
            "system_chars": len(system_content),
            "diff": diff,
            "diff_chars": len(diff),
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
