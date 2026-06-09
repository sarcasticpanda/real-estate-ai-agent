"""
Batch-embed all properties and upload to Supabase.

Usage:
    python embeddings/generate_embeddings.py            # embed all properties
    python embeddings/generate_embeddings.py --limit 5  # test with 5 properties
    python embeddings/generate_embeddings.py --force    # re-embed everything

Prerequisites:
    1. Supabase tables created (run supabase/migrations/001_initial_schema.sql)
    2. .env has SUPABASE_URL and SUPABASE_KEY
    3. pip install sentence-transformers supabase
"""

import json
import sys
import argparse
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from embeddings.embedding_model import embed_batch, build_semantic_text
from database.supabase_client import get_client, upsert_property

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

RAG_JSON_PATH = ROOT / "csv_container" / "rag_documents" / "rag_properties.json"


def load_properties() -> list[dict]:
    with open(RAG_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_already_embedded_ids() -> set[str]:
    """Check which doc_ids are already in Supabase."""
    client = get_client()
    result = client.table("properties").select("id").execute()
    return {row["id"] for row in (result.data or [])}


def run(limit: int | None = None, force: bool = False) -> None:
    properties = load_properties()

    if not force:
        existing_ids = get_already_embedded_ids()
        properties = [p for p in properties if p["doc_id"] not in existing_ids]
        logger.info(f"Skipping {len(existing_ids)} already embedded properties")

    if limit:
        properties = properties[:limit]

    logger.info(f"Embedding {len(properties)} properties...")

    # Build semantic texts
    texts = [build_semantic_text(p) for p in properties]

    # Batch embed (fast — all in one shot via sentence-transformers)
    logger.info("Running embedding model...")
    embeddings = embed_batch(texts, batch_size=32)

    # Upload to Supabase one by one
    logger.info("Uploading to Supabase...")
    success = 0
    failed = 0
    for prop, text, embedding in zip(properties, texts, embeddings):
        try:
            upsert_property(prop, text, embedding)
            success += 1
            if success % 10 == 0:
                logger.info(f"  Uploaded {success}/{len(properties)}")
        except Exception as e:
            logger.error(f"Failed to upload {prop.get('doc_id')}: {e}")
            failed += 1

    logger.info(f"\nDone! Uploaded: {success}, Failed: {failed}")
    logger.info("Next step: create the ivfflat index in Supabase SQL editor:")
    logger.info("  create index on properties using ivfflat (embedding vector_cosine_ops) with (lists = 10);")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate and upload property embeddings")
    parser.add_argument("--limit", type=int, help="Process only N properties")
    parser.add_argument("--force", action="store_true", help="Re-embed already uploaded properties")
    args = parser.parse_args()
    run(limit=args.limit, force=args.force)
