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
        import httpx
        from langchain_ollama import ChatOllama

        # Ollama serialises inference: when both analysts run in parallel,
        # one queues behind the other. With chunked diffs (3+ chunks/agent),
        # the second-in-queue request can wait minutes before any byte
        # arrives — a single-float timeout (read=300s) trips on that idle
        # gap. read=None waits indefinitely for the local server; connect/
        # write/pool stay bounded so genuine network failures still fail fast.
        client_timeout = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=30.0)
        kwargs: dict = {
            "model": model_name,
            "temperature": cfg.temperature,
            "format": "json",
            "num_ctx": getattr(settings, cfg.num_ctx_setting),
            "num_predict": settings.ollama_num_predict_analyst,
            "keep_alive": settings.ollama_keep_alive,
            "async_client_kwargs": {"timeout": client_timeout},
            "client_kwargs": {"timeout": client_timeout},
        }
        # qwen3 is a reasoning model — default <think>...</think> prefix is
        # incompatible with format=json (model burns full num_predict on
        # thinking, never emits valid JSON). Ollama's API-level `think: false`
        # disables reasoning cleanly. Other models ignore this field.
        if "qwen3" in model_name.lower():
            kwargs["reasoning"] = False
        return ChatOllama(**kwargs)
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
    if provider == "mlx":
        # mlx_lm.server exposes an OpenAI-compatible /v1 endpoint. We reuse
        # ChatOpenAI with a local base_url; api_key is required by the SDK
        # but not validated server-side.
        #
        # max_tokens is critical: mlx_lm.server has no default cap, so a
        # reasoning model (qwen3 thinking) at temperature=0.0 can loop
        # indefinitely on `<think>...</think>` blocks. 4096 is enough for
        # ~15 structured comments and bounds worst-case latency.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            temperature=cfg.temperature,
            base_url=settings.mlx_base_url,
            api_key="not-needed",
            max_tokens=4096,
        )
    raise ValueError(
        f"Unknown TYPE_AGENTS={provider!r} (expected local|anthropic|gemini|openai|mlx)"
    )


def build_analyst(cfg: AnalystConfig, system_prompt: str):
    """Provider-agnostic analyst factory. Tools disabled by design.

    For local Ollama we rely on `format=json` at the API level + manual JSON
    parse in nodes/analyst.py — passing `response_format` here triggers
    tool-calling-based structured output, which qwen2.5:3b cannot produce
    reliably and ends up in an infinite retry loop inside create_agent.
    Cloud providers (Anthropic/Gemini/OpenAI) have robust native tool-calling,
    so structured output via response_format works and is preferred there.
    """
    from src.core.config import settings

    model_name = getattr(settings, cfg.model_setting, None)
    if not model_name:
        raise ValueError(f"{cfg.model_setting.upper()} is not set")

    provider = settings.type_agents or "local"

    # Qwen3 on mlx_lm.server: there's no API-level `reasoning=False` toggle
    # like Ollama has. The model defaults to <think>...</think> reasoning,
    # which at temperature=0.0 can loop deterministically and burn the
    # entire token budget without emitting structured output. Qwen3 honours
    # an in-prompt `/no_think` directive to disable reasoning cleanly.
    if provider == "mlx" and "qwen3" in model_name.lower():
        system_prompt = f"{system_prompt}\n\n/no_think"

    llm = _build_llm(cfg, model_name)
    agent_kwargs: dict = {
        "model": llm,
        "tools": [],
        "system_prompt": system_prompt,
        "name": cfg.log_label,
    }
    if provider != "local":
        agent_kwargs["response_format"] = cfg.response_format
    return create_agent(**agent_kwargs)
