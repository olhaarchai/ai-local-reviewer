import logging
import os

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

from src.agents.state import ReviewerState

load_dotenv()

logger = logging.getLogger(__name__)

# Initialize specialists
# Qwen for logic/security
llm_security = ChatOllama(model=os.getenv("OLLAMA_MODEL_SECURITY"), temperature=0)
# Mistral for style/best practices
llm_style = ChatOllama(model=os.getenv("OLLAMA_MODEL_STYLE"), temperature=0.2)
# Llama for final summary
llm_fast = ChatOllama(model=os.getenv("OLLAMA_MODEL_FAST"), temperature=0.1)


async def security_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_SECURITY")
    logger.info("[security_analyst] Starting analysis with model=%s", model_name)
    diff = state["diff"]
    prompt = f"Analyze for security bugs and vulnerabilities:\n{diff}"
    response = await llm_security.ainvoke(prompt)
    logger.info("[security_analyst] Done. Response length=%d chars", len(response.content))
    logger.debug("[security_analyst] Response:\n%s", response.content)
    return {"messages": [response]}


async def style_analyst_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_STYLE")
    logger.info("[style_analyst] Starting analysis with model=%s", model_name)
    diff = state["diff"]
    prompt = (
        "You are a Senior Developer. Review this code for style, naming conventions, "
        "and best practices. Suggest improvements for readability.\n\n"
        f"DIFF:\n{diff}"
    )
    response = await llm_style.ainvoke(prompt)
    logger.info("[style_analyst] Done. Response length=%d chars", len(response.content))
    logger.debug("[style_analyst] Response:\n%s", response.content)
    return {"messages": [response]}


async def summary_node(state: ReviewerState):
    model_name = os.getenv("OLLAMA_MODEL_FAST")
    logger.info("[summarizer] Starting summary with model=%s, total messages in state=%d", model_name, len(state["messages"]))

    # Collect all analyst outputs from state messages (exclude HumanMessages)
    analyst_outputs = [
        msg.content for msg in state["messages"]
        if hasattr(msg, "content") and msg.content
    ]
    combined = "\n\n---\n\n".join(analyst_outputs)
    logger.debug("[summarizer] Combined analyst input:\n%s", combined)

    prompt = (
        "You are a senior engineering lead. Based on the following security and style reviews, "
        "write a concise executive summary of the PR review findings.\n\n"
        f"REVIEWS:\n{combined}"
    )
    response = await llm_fast.ainvoke(prompt)
    logger.info("[summarizer] Done. Response length=%d chars", len(response.content))
    logger.debug("[summarizer] Response:\n%s", response.content)
    return {"messages": [response]}
