import logging
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.core.config import settings
from src.integrations.retriever import retriever_node
from src.review.agents.analyst import ANALYSTS
from src.review.nodes import (
    analyst_node,
    critic_node,
    filter_node,
    hitl_gate_node,
    linter_node,
    retry_node,
    summary_node,
)
from src.review.state import ReviewerState
from src.review.utils import state_get

builder = StateGraph(ReviewerState)

logger = logging.getLogger(__name__)


def _route_after_critic(state: ReviewerState | dict) -> str:
    is_valid = bool(state_get(state, "is_valid", False))
    iterations = int(state_get(state, "iterations", 0) or 0)
    if is_valid or iterations >= settings.max_critic_iterations:
        return "hitl_gate"
    return "retry"


def _route_after_hitl(state: ReviewerState | dict) -> str:
    return "retry" if state_get(state, "route") == "retry" else "summarizer"


async def enter_checkpointer(exit_stack: AsyncExitStack):
    postgres_dsn = settings.checkpoint_postgres_dsn
    if postgres_dsn:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "CHECKPOINT_POSTGRES_DSN set but langgraph-checkpoint-postgres is not installed."
            ) from exc

        return await exit_stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(postgres_dsn)
        )
    sqlite_path = settings.checkpoint_sqlite_path
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ModuleNotFoundError:
        logger.warning(
            "[checkpointer] SqliteSaver not available; falling back to InMemorySaver. "
            "Install langgraph-checkpoint-sqlite for persistence."
        )
        return InMemorySaver()
    path = Path(sqlite_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return await exit_stack.enter_async_context(
        AsyncSqliteSaver.from_conn_string(path.as_posix())
    )


builder.add_node("filter", filter_node)
builder.add_node("retriever", retriever_node)
builder.add_node("summarizer", summary_node)

agent_map = {
    key: (cfg.log_label, partial(analyst_node, key)) for key, cfg in ANALYSTS.items()
}
enabled_agents = [a for a in settings.enabled_agents if a in agent_map]

builder.add_edge(START, "filter")
builder.add_edge("filter", "retriever")

if enabled_agents:
    # Linter (ruff) runs between retriever and analysts — feeds lint_findings
    # into their system prompts so the LLM doesn't rederive deterministic issues.
    builder.add_node("linter", linter_node)
    builder.add_node("critic", critic_node)
    builder.add_node("retry", retry_node)
    builder.add_node("hitl_gate", hitl_gate_node)

    builder.add_edge("retriever", "linter")

    for key in enabled_agents:
        node_name, node_fn = agent_map[key]
        builder.add_node(node_name, node_fn)
        builder.add_edge("linter", node_name)
        builder.add_edge(node_name, "critic")

    builder.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "retry": "retry",
            "hitl_gate": "hitl_gate",
        },
    )
    builder.add_conditional_edges(
        "hitl_gate",
        _route_after_hitl,
        {
            "retry": "retry",
            "summarizer": "summarizer",
        },
    )
    for key in enabled_agents:
        node_name, _ = agent_map[key]
        builder.add_edge("retry", node_name)
else:
    logger.warning("[graph] No enabled agents; skipping analysis")
    builder.add_edge("retriever", "summarizer")

builder.add_edge("summarizer", END)


def build_reviewer_app(checkpointer, *, interrupt_before=None):
    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)
