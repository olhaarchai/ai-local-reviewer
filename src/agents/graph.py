from langgraph.graph import END, START, StateGraph

from src.agents.nodes import (
    critic_node,
    filter_node,
    security_analyst_node,
    style_analyst_node,
    summary_node,
)
from src.agents.retriever import retriever_node
from src.agents.state import ReviewerState

builder = StateGraph(ReviewerState)

builder.add_node("filter", filter_node)
builder.add_node("retriever", retriever_node)
builder.add_node("security_analyst", security_analyst_node)
builder.add_node("style_analyst", style_analyst_node)
builder.add_node("critic", critic_node)
builder.add_node("summarizer", summary_node)

builder.add_edge(START, "filter")
builder.add_edge("filter", "retriever")
builder.add_edge("retriever", "security_analyst")
builder.add_edge("retriever", "style_analyst")

builder.add_edge("security_analyst", "critic")
builder.add_edge("style_analyst", "critic")

builder.add_edge("critic", "summarizer")
builder.add_edge("summarizer", END)

reviewer_app = builder.compile()
