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
    "sh": "shell-scripts",
}
_ALWAYS_INCLUDE = ["security-owasp", "api-design"]
_COLLECTION = "code_review_rules"

# Path-pattern rules for YAML files — checked in order, first match wins.
_YAML_PATH_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.github/workflows/"), "github-actions"),
    (re.compile(r"docker-compose"), "docker"),
    (re.compile(r"(^|/)ansible/|(^|/)playbooks/|(^|/)roles/"), "ansible"),
    (re.compile(r"(Chart\.yaml|/templates/.*\.yaml|values.*\.yaml)"), "kubernetes"),
]

# Loaded once at import time — avoids reloading 80 MB weights on every PR review.
_embed_model = SentenceTransformer("all-MiniLM-L6-v2")
logger.info("[retriever] Embedding model loaded: all-MiniLM-L6-v2")


def _classify_file(path: str) -> str | None:
    """Return the rules category for a file path, or None if unknown."""
    if not path:
        return None
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("yaml", "yml"):
        for pattern, category in _YAML_PATH_RULES:
            if pattern.search(path):
                return category
        return None  # unrecognised YAML — inject no rules rather than wrong ones
    return _EXT_TO_CATEGORY.get(ext)


def extract_pr_files(diff: str) -> list[str]:
    """Extract changed file paths from diff --git headers only."""
    return re.findall(r"^diff --git a/\S+ b/(\S+)", diff, re.MULTILINE)


def detect_stack(diff: str) -> list[str]:
    """Detect tech categories from actual changed file paths, not diff body content."""
    paths = extract_pr_files(diff)
    detected = list({c for p in paths if (c := _classify_file(p)) is not None})
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

        file_paths = extract_pr_files(diff)
        paths_str = ", ".join(file_paths[:10])
        search_query = (
            f"Stack: {', '.join(detected_stack)}. "
            f"Files: {paths_str}. "
            f"Changes: {diff[:400]}"
        )
        logger.debug("[retriever] Enriched query: %s", search_query[:120])

        query_embedding = _embed_model.encode([search_query]).tolist()

        all_guidelines: list[str] = []
        for cat in detected_stack:
            cat_results = collection.search(
                data=query_embedding,
                anns_field="embedding",
                param={"metric_type": "L2", "params": {"nprobe": 10}},
                limit=settings.milvus_rules_per_category,
                expr=f'category == "{cat}"',
                output_fields=["text"],
            )
            hits = [
                hit.entity.get("text")
                for hit in cat_results[0]
                if hit.entity.get("text")
            ]
            logger.info("[RAG] Category '%s': found %d rules", cat, len(hits))
            all_guidelines.extend(hits)

        tech_cats = [c for c in detected_stack if c not in _ALWAYS_INCLUDE]
        context_lines = "\n".join(f"  - {p}" for p in file_paths[:20])
        stack_context = (
            (
                f"PR files ({len(file_paths)} total):\n{context_lines}\n\n"
                f"Detected tech: {', '.join(tech_cats) or 'none'}"
            )
            if file_paths
            else ""
        )

        logger.info(
            "[retriever] Found %d relevant guidelines total", len(all_guidelines)
        )
        return {"guidelines": all_guidelines, "stack_context": stack_context}

    except Exception as e:
        logger.warning(
            "[retriever] Milvus unavailable (%s), continuing without rules", e
        )
        return {"guidelines": []}
