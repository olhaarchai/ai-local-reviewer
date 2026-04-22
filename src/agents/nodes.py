import json
import logging
import os

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from src.agents.state import ReviewerState

load_dotenv()

logger = logging.getLogger(__name__)

llm_security = ChatOllama(
    model=os.getenv("OLLAMA_MODEL_SECURITY"), temperature=0, format="json"
)
llm_style = ChatOllama(
    model=os.getenv("OLLAMA_MODEL_STYLE"), temperature=0.2, format="json"
)
llm_fast = ChatOllama(model=os.getenv("OLLAMA_MODEL_FAST"), temperature=0.1)

_FORMAT = 'Output ONLY a raw JSON array. No markdown, no explanation, no code blocks.\nFormat: [{"path": "file_path", "line": 10, "body": "description"}]'


def _parse_json_response(content: str, node: str) -> list:
    clean = content.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(clean)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"Unexpected JSON type: {type(parsed)}")
    logger.info("[%s] Parsed %d structured comments", node, len(parsed))
    return parsed


async def security_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_SECURITY")
    logger.info("[security_analyst] Starting with model=%s", model_name)

    response = await llm_security.ainvoke(
        [
            SystemMessage(
                content=(
                    "You are a Senior Security Engineer. Analyze the code diff for vulnerabilities. "
                    + _FORMAT
                )
            ),
            HumanMessage(content=f"DIFF:\n{state['diff']}"),
        ]
    )

    logger.debug("[security_analyst] Raw response:\n%s", response.content)

    try:
        comments = _parse_json_response(response.content, "security_analyst")
        return {"messages": [response], "comments": comments}
    except Exception as e:
        logger.error("[security_analyst] JSON parse failed: %s", e)
        return {"messages": [response]}


async def style_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_STYLE")
    logger.info("[style_analyst] Starting with model=%s", model_name)

    response = await llm_style.ainvoke(
        [
            SystemMessage(
                content=(
                    "You are a Senior Developer. Review the code diff for style, naming conventions, "
                    "and best practices. " + _FORMAT
                )
            ),
            HumanMessage(content=f"DIFF:\n{state['diff']}"),
        ]
    )

    logger.debug("[style_analyst] Raw response:\n%s", response.content)

    try:
        comments = _parse_json_response(response.content, "style_analyst")
        return {"messages": [response], "comments": comments}
    except Exception as e:
        logger.error("[style_analyst] JSON parse failed: %s", e)
        return {"messages": [response]}


async def summary_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_FAST")
    comments = state.get("comments", [])
    logger.info(
        "[summarizer] Starting with model=%s, total structured comments=%d",
        model_name,
        len(comments),
    )

    analyst_outputs = [
        msg.content
        for msg in state["messages"]
        if hasattr(msg, "content") and msg.content
    ]
    combined = "\n\n---\n\n".join(analyst_outputs)

    response = await llm_fast.ainvoke(
        [
            SystemMessage(
                content="You are a senior engineering lead. Write a concise executive summary of the PR review findings."
            ),
            HumanMessage(content=f"REVIEWS:\n{combined}"),
        ]
    )

    logger.info("[summarizer] Done. Response length=%d chars", len(response.content))
    logger.debug("[summarizer] Response:\n%s", response.content)
    return {"messages": [response]}
