import os

from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from src.agents.state import StyleReviewResult
from src.agents.tools import read_url, web_search


def build_style_agent(system_prompt: str):
    model_name = os.getenv("OLLAMA_MODEL_STYLE")
    if not model_name:
        raise ValueError("OLLAMA_MODEL_STYLE is not set")
    timeout = float(os.getenv("OLLAMA_REQUEST_TIMEOUT", "300"))
    llm_style = ChatOllama(
        model=model_name,
        temperature=0.2,
        format="json",
        async_client_kwargs={"timeout": timeout},
        client_kwargs={"timeout": timeout},
    )
    return create_agent(
        model=llm_style,
        tools=[web_search, read_url],
        system_prompt=system_prompt,
        response_format=StyleReviewResult,
        name="style_analyst",
    )
