import logging
import re
from typing import Any

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_EXT_TO_CATEGORY: dict[str, str] = {
    "py": "python-general",
    "ts": "typescript",
    "tsx": "react-nextjs",
    "jsx": "react-nextjs",
    "go": "golang",
    "rs": "rust",
    "tf": "terraform",
    "yaml": "kubernetes",
    "yml": "kubernetes",
    "sh": "shell-scripts",
}
_ALWAYS_INCLUDE = ["security-owasp", "api-design"]
_COLLECTION = "code_review_rules"
_SEARCH_LIMIT = 5

# Loaded once at import time — avoids reloading 80 MB weights on every PR review.
_embed_model = SentenceTransformer("all-MiniLM-L6-v2")
logger.info("[retriever] Embedding model loaded: all-MiniLM-L6-v2")


def detect_stack(diff: str) -> list[str]:
    extensions = re.findall(r"\.([a-z0-9]+)\b", diff)
    detected = list({_EXT_TO_CATEGORY[e] for e in extensions if e in _EXT_TO_CATEGORY})
    for cat in _ALWAYS_INCLUDE:
        if cat not in detected:
            detected.append(cat)
    return detected


def _state_get(state: dict | Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


async def retriever_node(state: dict | Any) -> dict:
    diff = _state_get(state, "diff", "")
    if not diff:
        return {"guidelines": []}

    detected_stack = detect_stack(diff)
    logger.info("[retriever] Stack detected: %s", detected_stack)

    try:
        from pymilvus import Collection, connections, utility

        from src.core.config import settings

        connections.connect(
            host=settings.milvus_host,
            port=str(settings.milvus_port),
        )

        if not utility.has_collection(_COLLECTION):
            logger.warning(
                "[retriever] Collection '%s' not found, skipping", _COLLECTION
            )
            return {"guidelines": []}

        collection = Collection(_COLLECTION)
        collection.load()

        file_paths = re.findall(r"^diff --git a/(.*) b/", diff, re.MULTILINE)
        paths_str = ", ".join(file_paths[:10])
        search_query = (
            f"Stack: {', '.join(detected_stack)}. "
            f"Files: {paths_str}. "
            f"Changes: {diff[:400]}"
        )
        logger.debug("[retriever] Enriched query: %s", search_query[:120])

        expr = " || ".join(f'category == "{cat}"' for cat in detected_stack)
        query_embedding = _embed_model.encode([search_query]).tolist()

        results = collection.search(
            data=query_embedding,
            anns_field="embedding",
            param={"metric_type": "L2", "params": {"nprobe": 10}},
            limit=_SEARCH_LIMIT,
            expr=expr,
            output_fields=["text"],
        )

        guidelines = [hit.entity.get("text") for hit in results[0]]
        logger.info("[retriever] Found %d relevant guidelines", len(guidelines))
        return {"guidelines": guidelines}

    except Exception as e:
        logger.warning(
            "[retriever] Milvus unavailable (%s), continuing without rules", e
        )
        return {"guidelines": []}
