"""Single analyst builder + registry of analyst configs.

Provider is selected globally via env TYPE_AGENTS=local|anthropic|gemini|openai.
The model name per agent lives in MODEL_SECURITY / MODEL_STYLE — provider-agnostic.
Ollama-only knobs (num_ctx, keep_alive, num_predict, format=json) apply only
when TYPE_AGENTS=local.

Adding a new analyst:
  1. Create prompts/agents/<key>/persona.md and schema.md (optional focus.md).
  2. Add a response_format Pydantic model in src/review/state.py.
  3. Add config field model_<key> (and ollama_num_ctx_<key>) in config.py.
  4. Add an entry to ANALYSTS below.
  5. Add <key> to ENABLED_AGENTS env var.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain.agents import create_agent

from src.review.state import SecurityReviewResult, StyleReviewResult


@dataclass(frozen=True)
class AnalystConfig:
    agent_key: str
    log_label: str
    model_setting: str
    num_ctx_setting: str
    temperature: float
    response_format: type
    prompt_dir: str


ANALYSTS: dict[str, AnalystConfig] = {
    "security": AnalystConfig(
        agent_key="security",
        log_label="security_analyst",
        model_setting="model_security",
        num_ctx_setting="ollama_num_ctx_security",
        temperature=0.0,
        response_format=SecurityReviewResult,
        prompt_dir="security",
    ),
    "style": AnalystConfig(
        agent_key="style",
        log_label="style_analyst",
        model_setting="model_style",
        num_ctx_setting="ollama_num_ctx_style",
        temperature=0.2,
        response_format=StyleReviewResult,
        prompt_dir="style",
    ),
}


def _build_llm(cfg: AnalystConfig, model_name: str):
    from src.core.config import settings

    provider = settings.type_agents or "local"
    if provider == "local":
        from langchain_ollama import ChatOllama

        timeout = settings.ollama_request_timeout
        return ChatOllama(
            model=model_name,
            temperature=cfg.temperature,
            format="json",
            num_ctx=getattr(settings, cfg.num_ctx_setting),
            num_predict=settings.ollama_num_predict_analyst,
            keep_alive=settings.ollama_keep_alive,
            async_client_kwargs={"timeout": timeout},
            client_kwargs={"timeout": timeout},
        )
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        return ChatAnthropic(
            model=model_name,
            temperature=cfg.temperature,
            api_key=settings.anthropic_api_key,
        )
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY is not set")
        return ChatGoogleGenerativeAI(
            model=model_name,
            temperature=cfg.temperature,
            google_api_key=settings.google_api_key,
        )
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        return ChatOpenAI(
            model=model_name,
            temperature=cfg.temperature,
            api_key=settings.openai_api_key,
        )
    raise ValueError(
        f"Unknown TYPE_AGENTS={provider!r} (expected local|anthropic|gemini|openai)"
    )


def build_analyst(cfg: AnalystConfig, system_prompt: str):
    """Provider-agnostic analyst factory. Tools disabled by design."""
    from src.core.config import settings

    model_name = getattr(settings, cfg.model_setting, None)
    if not model_name:
        raise ValueError(f"{cfg.model_setting.upper()} is not set")

    llm = _build_llm(cfg, model_name)
    return create_agent(
        model=llm,
        tools=[],
        system_prompt=system_prompt,
        response_format=cfg.response_format,
        name=cfg.log_label,
    )
