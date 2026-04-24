"""Single analyst builder + registry of analyst configs.

Adding a new analyst:
  1. Create prompts/agents/<key>/persona.md and schema.md (optional focus.md).
  2. Add a response_format Pydantic model in src/review/state.py.
  3. Add config fields ollama_model_<key> and ollama_num_ctx_<key> in config.py.
  4. Add an entry to ANALYSTS below.
  5. Add <key> to ENABLED_AGENTS env var.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_ollama import ChatOllama

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
        model_setting="ollama_model_security",
        num_ctx_setting="ollama_num_ctx_security",
        temperature=0.0,
        response_format=SecurityReviewResult,
        prompt_dir="security",
    ),
    "style": AnalystConfig(
        agent_key="style",
        log_label="style_analyst",
        model_setting="ollama_model_style",
        num_ctx_setting="ollama_num_ctx_style",
        temperature=0.2,
        response_format=StyleReviewResult,
        prompt_dir="style",
    ),
}


def build_analyst(cfg: AnalystConfig, system_prompt: str):
    """ChatOllama + create_agent factory. No tools by design (see feat/improvements-2)."""
    from src.core.config import settings

    model_name = getattr(settings, cfg.model_setting, None)
    if not model_name:
        raise ValueError(f"{cfg.model_setting.upper()} is not set")
    timeout = settings.ollama_request_timeout
    llm = ChatOllama(
        model=model_name,
        temperature=cfg.temperature,
        format="json",
        num_ctx=getattr(settings, cfg.num_ctx_setting),
        num_predict=settings.ollama_num_predict_analyst,
        keep_alive=settings.ollama_keep_alive,
        async_client_kwargs={"timeout": timeout},
        client_kwargs={"timeout": timeout},
    )
    return create_agent(
        model=llm,
        tools=[],
        system_prompt=system_prompt,
        response_format=cfg.response_format,
        name=cfg.log_label,
    )
