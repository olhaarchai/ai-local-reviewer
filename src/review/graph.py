import logging
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.core.config import settings
from src.integrations.retriever import retriever_node
from src.review.nodes import (
    critic_node,
    filter_node,
    retry_node,
    security_analyst_node,
    style_analyst_node,
    summary_node,
)
from src.review.state import ReviewerState

builder = StateGraph(ReviewerState)

MAX_CRITIC_ITERATIONS = 3

logger = logging.getLogger(__name__)


def _state_get(state: ReviewerState | dict, key: str, default=None):
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _route_after_critic(state: ReviewerState | dict) -> str:
    is_valid = bool(_state_get(state, "is_valid", False))
    iterations = int(_state_get(state, "iterations", 0) or 0)
    if is_valid or iterations >= MAX_CRITIC_ITERATIONS:
        return "summarizer"
    return "retry"


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
builder.add_node("security_analyst", security_analyst_node)
builder.add_node("style_analyst", style_analyst_node)
builder.add_node("critic", critic_node)
builder.add_node("retry", retry_node)
builder.add_node("summarizer", summary_node)

builder.add_edge(START, "filter")
builder.add_edge("filter", "retriever")
builder.add_edge("retriever", "security_analyst")
builder.add_edge("retriever", "style_analyst")

builder.add_edge("security_analyst", "critic")
builder.add_edge("style_analyst", "critic")

builder.add_conditional_edges(
    "critic",
    _route_after_critic,
    {
        "retry": "retry",
        "summarizer": "summarizer",
    },
)
builder.add_edge("retry", "security_analyst")
builder.add_edge("retry", "style_analyst")
builder.add_edge("summarizer", END)


def build_reviewer_app(checkpointer, *, interrupt_before=None):
    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)
