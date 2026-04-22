from langgraph.graph import END, START, StateGraph

from src.agents.nodes import (
    critic_node,
    filter_node,
    retry_node,
    security_analyst_node,
    style_analyst_node,
    summary_node,
)
from src.agents.retriever import retriever_node
from src.agents.state import ReviewerState

builder = StateGraph(ReviewerState)

MAX_CRITIC_ITERATIONS = 3


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

reviewer_app = builder.compile()
