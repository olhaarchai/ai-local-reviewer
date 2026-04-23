import os

from langchain.agents import create_agent
from langchain_ollama import ChatOllama


def build_summarizer_agent(system_prompt: str):
    model_name = os.getenv("OLLAMA_MODEL_FAST")
    if not model_name:
        raise ValueError("OLLAMA_MODEL_FAST is not set")
    timeout = float(os.getenv("OLLAMA_REQUEST_TIMEOUT", "300"))
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
