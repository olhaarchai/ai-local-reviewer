from langgraph.graph import END, START, StateGraph

from src.agents.nodes import security_analyst_node, style_analyst_node, summary_node
from src.agents.state import ReviewerState

builder = StateGraph(ReviewerState)

# 1. Add all nodes
builder.add_node("security_analyst", security_analyst_node)
builder.add_node("style_analyst", style_analyst_node)
builder.add_node("summarizer", summary_node)

# 2. Define flow: First both analysts work, then summarizer
builder.add_edge(START, "security_analyst")
builder.add_edge(START, "style_analyst")  # Start both in parallel

builder.add_edge("security_analyst", "summarizer")
builder.add_edge("style_analyst", "summarizer")

builder.add_edge("summarizer", END)

reviewer_app = builder.compile()
