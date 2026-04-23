import os

from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from src.agents.state import SecurityReviewResult
from src.agents.tools import read_url, web_search


def build_security_agent(system_prompt: str):
    model_name = os.getenv("OLLAMA_MODEL_SECURITY")
    if not model_name:
        raise ValueError("OLLAMA_MODEL_SECURITY is not set")
    timeout = float(os.getenv("OLLAMA_REQUEST_TIMEOUT", "300"))
    llm_security = ChatOllama(
        model=model_name,
        temperature=0,
        format="json",
        async_client_kwargs={"timeout": timeout},
        client_kwargs={"timeout": timeout},
    )
    return create_agent(
        model=llm_security,
        tools=[web_search, read_url],
        system_prompt=system_prompt,
        response_format=SecurityReviewResult,
        name="security_analyst",
    )
