import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)
from sentence_transformers import SentenceTransformer

load_dotenv()

COLLECTION = "code_review_rules"
DIM = 384
RULES_DIR = Path(__file__).parent.parent / "rules"

MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")


def parse_rules(path: Path) -> list[str]:
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            rule = line[2:].strip()
            if rule:
                rules.append(rule)
    return rules


def main() -> None:
    print(f"Connecting to Milvus at {MILVUS_HOST}:{MILVUS_PORT}...")
    try:
        connections.connect(host=MILVUS_HOST, port=MILVUS_PORT)
    except Exception as e:
        print(f"[ERROR] Cannot connect to Milvus: {e}", file=sys.stderr)
        sys.exit(1)

    if utility.has_collection(COLLECTION):
        utility.drop_collection(COLLECTION)
        print(f"Dropped existing collection '{COLLECTION}'")

    schema = CollectionSchema(
        [
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema("text", DataType.VARCHAR, max_length=2048),
            FieldSchema("category", DataType.VARCHAR, max_length=128),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=DIM),
        ],
        description="Code review rules indexed by tech category",
    )
    collection = Collection(COLLECTION, schema)

    rule_files = sorted(RULES_DIR.glob("*.md"))
    if not rule_files:
        print(f"[ERROR] No .md files found in {RULES_DIR}", file=sys.stderr)
        sys.exit(1)

    texts: list[str] = []
    categories: list[str] = []
    for path in rule_files:
        category = path.stem
        rules = parse_rules(path)
        texts.extend(rules)
        categories.extend([category] * len(rules))
        print(f"  {category}: {len(rules)} rules")

    print(f"\nEmbedding {len(texts)} rules with all-MiniLM-L6-v2...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    embeddings = model.encode(texts, show_progress_bar=True).tolist()

    collection.insert([texts, categories, embeddings])

    collection.create_index(
        "embedding",
        {"index_type": "FLAT", "metric_type": "L2"},
    )
    collection.load()

    print(
        f"\nIngested {len(texts)} rules from {len(rule_files)} files into '{COLLECTION}'"
    )


if __name__ == "__main__":
    main()
