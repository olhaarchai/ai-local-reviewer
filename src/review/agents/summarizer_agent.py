from langchain.agents import create_agent
from langchain_ollama import ChatOllama


def build_summarizer_agent(system_prompt: str):
    from src.core.config import settings

    model_name = settings.ollama_model_fast
    if not model_name:
        raise ValueError("OLLAMA_MODEL_FAST is not set")
    timeout = settings.ollama_request_timeout
    llm = ChatOllama(
        model=model_name,
        temperature=0.1,
        async_client_kwargs={"timeout": timeout},
        client_kwargs={"timeout": timeout},
    )
    return create_agent(
        model=llm,
        tools=[],
        system_prompt=system_prompt,
        name="summarizer",
    )
