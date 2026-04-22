from typing import Annotated, List, TypedDict

from langgraph.graph.message import add_messages


class ReviewerState(TypedDict):
    # Raw diff string from GitHub
    diff: str
    # Structured comments to be sent to GitHub later
    # Format: {"path": str, "line": int, "body": str}
    comments: List[dict]
    # AI conversation history
    messages: Annotated[list, add_messages]
