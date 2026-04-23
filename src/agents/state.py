import operator
from typing import Annotated, Any, Literal, Optional, Union

from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field


class SecurityComment(BaseModel):
    path: str
    line: int
    body: str
    owasp_id: Optional[str] = None
    severity: Optional[Literal["Critical", "High", "Medium", "Low"]] = None


class StyleComment(BaseModel):
    path: str
    line: int
    body: str


class SecurityReviewResult(BaseModel):
    comments: list[SecurityComment] = Field(default_factory=list)


class StyleReviewResult(BaseModel):
    comments: list[StyleComment] = Field(default_factory=list)


ReviewComment = Union[SecurityComment, StyleComment, dict[str, Any]]


class ReviewerState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    diff: str
    comments: Annotated[list[ReviewComment], operator.add] = Field(default_factory=list)
    messages: Annotated[list[Any], add_messages] = Field(default_factory=list)
    guidelines: list[str] = Field(default_factory=list)
    iterations: int = 0
    is_valid: bool = False
    critic_feedback: Optional[str] = None
    lint_findings: list[str] = Field(default_factory=list)
    raw_responses: Annotated[list[str], operator.add] = Field(default_factory=list)
    route: Optional[str] = None
    critic_issues: list[dict[str, Any]] = Field(default_factory=list)
    summary_override: Optional[str] = None
    timings: Annotated[list[dict[str, float]], operator.add] = Field(
        default_factory=list
    )
