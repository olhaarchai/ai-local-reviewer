import operator
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ReviewerState(TypedDict):
    diff: str
    comments: Annotated[list, operator.add]
    messages: Annotated[list, add_messages]
