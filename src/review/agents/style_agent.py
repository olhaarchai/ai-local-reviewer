
from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from src.review.state import StyleReviewResult
from src.tools.web_tools import read_url, web_search


def build_style_agent(system_prompt: str):
    from src.core.config import settings

    model_name = settings.ollama_model_style
    if not model_name:
        raise ValueError("OLLAMA_MODEL_STYLE is not set")
    timeout = settings.ollama_request_timeout
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
