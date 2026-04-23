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
    milvus_rules_per_category: int
    milvus_score_threshold: float

    web_search_max_results: int
    read_url_max_chars: int

    max_critic_iterations: int
    agent_recursion_limit: int

    enabled_agents: list[str]

    log_level: str

    use_reranker: bool
    reranker_model: str
    dense_overfetch_multiplier: int
    bm25_enabled: bool
    linter_enabled: bool
    linter_max_findings_per_file: int
    linter_max_findings_total: int
    hitl_auto_approve: bool
    output_dir: str
    ollama_num_ctx_security: int
    ollama_num_ctx_style: int
    ollama_num_ctx_fast: int
    ollama_keep_alive: str
    ollama_num_predict_analyst: int
    ollama_num_predict_fast: int


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
    milvus_rules_per_category=_get_int("MILVUS_RULES_PER_CATEGORY", 4),
    milvus_score_threshold=_get_float("MILVUS_SCORE_THRESHOLD", 1.5),
    web_search_max_results=_get_int("WEB_SEARCH_MAX_RESULTS", 5),
    read_url_max_chars=_get_int("READ_URL_MAX_CHARS", 5000),
    max_critic_iterations=_get_int("MAX_CRITIC_ITERATIONS", 3),
    agent_recursion_limit=_get_int("AGENT_RECURSION_LIMIT", 2),
    enabled_agents=_get_csv("ENABLED_AGENTS", ["security", "style"]),
    log_level=os.getenv("LOG_LEVEL", "INFO"),
    use_reranker=_get_bool("USE_RERANKER", False),
    reranker_model=os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    dense_overfetch_multiplier=_get_int("DENSE_OVERFETCH_MULTIPLIER", 3),
    bm25_enabled=_get_bool("BM25_ENABLED", True),
    linter_enabled=_get_bool("LINTER_ENABLED", True),
    linter_max_findings_per_file=_get_int("LINTER_MAX_FINDINGS_PER_FILE", 3),
    linter_max_findings_total=_get_int("LINTER_MAX_FINDINGS_TOTAL", 10),
    hitl_auto_approve=_get_bool("HITL_AUTO_APPROVE", False),
    output_dir=os.getenv("OUTPUT_DIR", "output"),
    # Ollama's default num_ctx is 2048 — far below our typical diff + rules
    # + system prompt (~10k tokens). Raise per-model to avoid silent input
    # truncation, which causes hallucinations and stalls on 7B models.
    ollama_num_ctx_security=_get_int("OLLAMA_NUM_CTX_SECURITY", 16384),
    ollama_num_ctx_style=_get_int("OLLAMA_NUM_CTX_STYLE", 8192),
    ollama_num_ctx_fast=_get_int("OLLAMA_NUM_CTX_FAST", 4096),
    # How long Ollama keeps the model loaded after a request. Stops cold-
    # start penalty between PRs. Accepts "30m", "1h", "24h", "-1" (forever).
    ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
    # Hard cap on generated tokens. Without this, Ollama's default -1
    # lets the model loop into thousands of tokens of JSON garbage on
    # long-context format="json" runs — a well-known qwen2.5 failure mode.
    ollama_num_predict_analyst=_get_int("OLLAMA_NUM_PREDICT_ANALYST", 2048),
    ollama_num_predict_fast=_get_int("OLLAMA_NUM_PREDICT_FAST", 1024),
)
