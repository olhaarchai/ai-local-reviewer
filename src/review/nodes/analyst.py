"""Single analyst node — thin dispatcher to AnalystRunner.

graph.py wires one `partial(analyst_node, key)` per enabled agent. All
prompt composition, diff chunking, LLM invocation, parsing, and trace
construction live in `src/review/agents/runner.py` so this module stays
trivial. Adding an analyst = adding an ANALYSTS entry + prompts.
"""

from __future__ import annotations

from src.review.agents.runner import build_runner
from src.review.state import ReviewerState


async def analyst_node(
    agent_key: str, state: ReviewerState, config: dict | None = None
) -> dict:
    """Run the analyst whose key matches `agent_key` in the ANALYSTS registry."""
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id") or "default"
    return await build_runner(agent_key, state, thread_id).run()
