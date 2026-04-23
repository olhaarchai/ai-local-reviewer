from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from src.review.state import SecurityReviewResult
from src.tools.web_tools import read_url, web_search


def build_security_agent(system_prompt: str):
    from src.core.config import settings

    model_name = settings.ollama_model_security
    if not model_name:
        raise ValueError("OLLAMA_MODEL_SECURITY is not set")
    timeout = settings.ollama_request_timeout
    llm_security = ChatOllama(
        model=model_name,
        temperature=0,
        format="json",
        num_ctx=settings.ollama_num_ctx_security,
        num_predict=settings.ollama_num_predict_analyst,
        keep_alive=settings.ollama_keep_alive,
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
