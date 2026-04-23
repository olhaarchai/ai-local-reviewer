import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    items = [item.strip() for item in raw.split(",")]
    return [item for item in items if item]


@dataclass(frozen=True)
class Settings:
    github_webhook_secret: str | None
    github_app_id: str | None
    github_private_key_path: str | None
    github_bot_name: str | None

    ollama_model_security: str | None
    ollama_model_style: str | None
    ollama_model_fast: str | None
    ollama_base_url: str | None
    ollama_request_timeout: float

    summarizer_use_llm: bool

    checkpoint_postgres_dsn: str | None
    checkpoint_sqlite_path: str

    milvus_host: str
    milvus_port: int

    web_search_max_results: int
    read_url_max_chars: int

    max_critic_iterations: int
    agent_recursion_limit: int

    enabled_agents: list[str]

    log_level: str


settings = Settings(
    github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET"),
    github_app_id=os.getenv("GITHUB_APP_ID"),
    github_private_key_path=os.getenv("GITHUB_PRIVATE_KEY_PATH"),
    github_bot_name=os.getenv("GITHUB_BOT_NAME"),
    ollama_model_security=os.getenv("OLLAMA_MODEL_SECURITY"),
    ollama_model_style=os.getenv("OLLAMA_MODEL_STYLE"),
    ollama_model_fast=os.getenv("OLLAMA_MODEL_FAST"),
    ollama_base_url=os.getenv("OLLAMA_BASE_URL"),
    ollama_request_timeout=_get_float("OLLAMA_REQUEST_TIMEOUT", 300.0),
    summarizer_use_llm=_get_bool("SUMMARIZER_USE_LLM", False),
    checkpoint_postgres_dsn=os.getenv("CHECKPOINT_POSTGRES_DSN"),
    checkpoint_sqlite_path=os.getenv(
        "CHECKPOINT_SQLITE_PATH", ".data/reviewer_checkpoints.sqlite"
    ),
    milvus_host=os.getenv("MILVUS_HOST", "localhost"),
    milvus_port=_get_int("MILVUS_PORT", 19530),
    web_search_max_results=_get_int("WEB_SEARCH_MAX_RESULTS", 5),
    read_url_max_chars=_get_int("READ_URL_MAX_CHARS", 5000),
    max_critic_iterations=_get_int("MAX_CRITIC_ITERATIONS", 3),
    agent_recursion_limit=_get_int("AGENT_RECURSION_LIMIT", 2),
    enabled_agents=_get_csv("ENABLED_AGENTS", ["security", "style"]),
    log_level=os.getenv("LOG_LEVEL", "INFO"),
)
