import logging
import re
from typing import Any

from sentence_transformers import SentenceTransformer

from src.integrations.sparse_index import SPARSE_INDEX, tokenize
from src.review.utils import extract_pr_files, state_get

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
_ALWAYS_INCLUDE = ["security-owasp", "api-design", "common"]
# Extra categories a file accepts rules from, beyond its primary _EXT_TO_CATEGORY.
# Example: a `.tsx` file's primary category is `react-nextjs`, but TypeScript
# rules (TS001, TS004, …) clearly apply too because .tsx IS TypeScript. Without
# this map, critic G4 rejects correct findings like `[TS001] any` on a .tsx.
_COMPATIBLE_CATEGORIES: dict[str, set[str]] = {
    "react-nextjs": {"typescript"},
}
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

# Cross-encoder reranker is loaded lazily — see _get_reranker().
_reranker = None
_reranker_failed = False


def _get_reranker():
    """Lazy-load cross-encoder only if the setting is enabled."""
    global _reranker, _reranker_failed
    if _reranker is not None or _reranker_failed:
        return _reranker
    try:
        from sentence_transformers import CrossEncoder

        from src.core.config import settings

        _reranker = CrossEncoder(settings.reranker_model)
        logger.info("[retriever] Reranker loaded: %s", settings.reranker_model)
    except Exception as exc:
        _reranker_failed = True
        logger.warning("[retriever] Reranker load failed (%s); disabling", exc)
    return _reranker


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


def detect_stack(diff: str) -> list[str]:
    """Detect tech categories from actual changed file paths, not diff body content."""
    paths = extract_pr_files(diff)
    detected = list({c for p in paths if (c := _classify_file(p)) is not None})
    for cat in _ALWAYS_INCLUDE:
        if cat not in detected:
            detected.append(cat)
    return detected


def _rrf_merge(
    dense: list[tuple[str, float]],
    sparse: list[tuple[str, float]],
    k: int = 60,
) -> list[str]:
    """RRF over rank-lists keyed on rule text. Returns keys sorted desc by fused score."""
    scores: dict[str, float] = {}
    for rank, (key, _) in enumerate(dense):
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    for rank, (key, _) in enumerate(sparse):
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.keys(), key=lambda x: scores[x], reverse=True)


async def retriever_node(state: dict | Any) -> dict:
    diff = state_get(state, "diff", "")
    if not diff:
        return {"guidelines": [], "rag_trace": []}

    from src.core.config import settings
    from src.review.state import Guideline

    detected_stack = detect_stack(diff)
    logger.info("[retriever] Stack detected: %s", detected_stack)

    file_paths = extract_pr_files(diff)
    paths_str = ", ".join(file_paths[:10])
    search_query = (
        f"Stack: {', '.join(detected_stack)}. Files: {paths_str}. Changes: {diff[:400]}"
    )
    bm25_tokens = tokenize(search_query)
    logger.info(
        "[retriever] query: %r (%d chars)", search_query[:200], len(search_query)
    )
    logger.info("[retriever] bm25_tokens (first 15): %s", bm25_tokens[:15])

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

    over_fetch_mult = max(1, settings.dense_overfetch_multiplier)
    per_cat_final = settings.milvus_rules_per_category
    per_cat_over = per_cat_final * over_fetch_mult
    reranker = _get_reranker() if settings.use_reranker else None

    # Dense retrieval via Milvus — guarded so BM25 keeps working if Milvus is down.
    dense_by_cat: dict[str, list[dict]] = {cat: [] for cat in detected_stack}
    milvus_ok = False
    try:
        from pymilvus import Collection, connections, utility

        connections.connect(
            host=settings.milvus_host,
            port=str(settings.milvus_port),
        )
        if not utility.has_collection(_COLLECTION):
            logger.warning(
                "[retriever] Collection '%s' not found, skipping dense", _COLLECTION
            )
        else:
            collection = Collection(_COLLECTION)
            collection.load()
            query_embedding = _embed_model.encode([search_query]).tolist()
            for cat in detected_stack:
                cat_results = collection.search(
                    data=query_embedding,
                    anns_field="embedding",
                    param={"metric_type": "L2", "params": {"nprobe": 10}},
                    limit=per_cat_over,
                    expr=f'category == "{cat}"',
                    output_fields=["text"],
                )
                hits = [
                    {"text": text, "distance": float(hit.distance)}
                    for hit in cat_results[0]
                    if (text := hit.entity.get("text"))
                    and hit.distance <= settings.milvus_score_threshold
                ]
                dense_by_cat[cat] = hits
            milvus_ok = True
    except Exception as e:
        logger.warning(
            "[retriever] Milvus unavailable (%s), falling back to BM25-only", e
        )

    all_guidelines: list[Guideline] = []
    bm25_enabled = settings.bm25_enabled
    rag_trace: list[dict[str, Any]] = []
    # Shared inputs snapshot — identical for every category, inlined once per entry
    # so the review log is self-contained when read glanced.
    shared_inputs = {
        "detected_stack": detected_stack,
        "file_paths": file_paths[:20],
        "search_query": search_query,
        "bm25_tokens": bm25_tokens[:30],
        "milvus_score_threshold": settings.milvus_score_threshold,
        "milvus_rules_per_category": per_cat_final,
        "milvus_overfetch_multiplier": over_fetch_mult,
        "bm25_enabled": bm25_enabled,
        "use_reranker": settings.use_reranker,
        "milvus_ok": milvus_ok,
    }

    for cat in detected_stack:
        dense_hits = dense_by_cat.get(cat, [])
        dense_keys: list[tuple[str, float]] = [
            (h["text"], h["distance"]) for h in dense_hits
        ]

        sparse_keys: list[tuple[str, float]] = []
        if bm25_enabled:
            sparse_pairs = SPARSE_INDEX.search(
                search_query, k=per_cat_over, category=cat
            )
            sparse_keys = [
                (SPARSE_INDEX.get(idx)["text"], score) for idx, score in sparse_pairs
            ]

        # Fallback: if Milvus is unreachable AND BM25 empty/disabled, skip category.
        if not dense_keys and not sparse_keys:
            logger.info("[RAG] Category '%s': dense_hits=0 bm25_hits=0 fused=0", cat)
            rag_trace.append(
                {
                    "category": cat,
                    "inputs": shared_inputs,
                    "dense_hits": [],
                    "sparse_hits": [],
                    "fused_order": [],
                    "kept": [],
                    "reranked": False,
                }
            )
            continue

        if dense_keys and sparse_keys:
            fused_texts = _rrf_merge(dense_keys, sparse_keys)
        elif dense_keys:
            fused_texts = [t for t, _ in dense_keys]
        else:
            fused_texts = [t for t, _ in sparse_keys]

        reranked = False
        if reranker is not None and len(fused_texts) > 1:
            try:
                pairs = [(search_query, t) for t in fused_texts]
                scores = reranker.predict(pairs)
                ranked = sorted(
                    zip(scores, fused_texts), key=lambda x: x[0], reverse=True
                )
                fused_texts = [t for _, t in ranked]
                reranked = True
            except Exception as exc:
                logger.warning("[retriever] Reranker failed (%s), skipping", exc)

        top_texts = fused_texts[:per_cat_final]
        all_guidelines.extend(Guideline.from_text(t, cat) for t in top_texts)

        rag_trace.append(
            {
                "category": cat,
                "inputs": shared_inputs,
                "dense_hits": [{"text": t, "distance": d} for t, d in dense_keys],
                "sparse_hits": [{"text": t, "score": s} for t, s in sparse_keys],
                "fused_order": fused_texts,
                "kept": top_texts,
                "reranked": reranked,
            }
        )

        logger.info(
            "[RAG] Category '%s': dense_hits=%d bm25_hits=%d fused=%d kept=%d reranked=%s",
            cat,
            len(dense_keys),
            len(sparse_keys),
            len(fused_texts),
            len(top_texts),
            reranked,
        )
        for i, (text, dist) in enumerate(dense_keys[:3], 1):
            logger.info("[RAG]   %s dense#%d d=%.3f: %s", cat, i, dist, text[:120])
        for i, (text, score) in enumerate(sparse_keys[:3], 1):
            logger.info("[RAG]   %s bm25#%d s=%.3f: %s", cat, i, score, text[:120])
        for i, text in enumerate(top_texts, 1):
            logger.info("[RAG]   %s KEPT#%d: %s", cat, i, text[:120])

    dense_total = sum(len(e["dense_hits"]) for e in rag_trace)
    bm25_total = sum(len(e["sparse_hits"]) for e in rag_trace)
    logger.info(
        "[retriever] cats=%s dense_total=%d bm25_total=%d kept=%d milvus_ok=%s rerank=%s",
        ",".join(detected_stack),
        dense_total,
        bm25_total,
        len(all_guidelines),
        milvus_ok,
        bool(reranker),
    )

    return {
        "guidelines": all_guidelines,
        "stack_context": stack_context,
        "rag_trace": rag_trace,
    }
