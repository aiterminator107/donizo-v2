#!/usr/bin/env python3
"""
Vector search over scraped Bricodépôt products.

Uses sentence-transformers (paraphrase-multilingual-MiniLM-L12-v2) for
embeddings and ChromaDB for persistence + approximate nearest-neighbour
queries.  Handles French and English queries natively — no LLM call at
search time.

CLI
---
    python search.py --build                      # index products
    python search.py --query "chauffe-eau 200L"   # search
    python search.py --query "floor tiles" -k 10  # top-10
    python search.py --stats                      # collection info
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer

from config import settings

EMBED_BATCH = 256

# ---------------------------------------------------------------------------
# Singleton model loader — avoids re-loading ~500 MB model on every call
# ---------------------------------------------------------------------------
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.embedding_model)
    return _model


def _get_collection(
    client: chromadb.ClientAPI | None = None,
) -> tuple[chromadb.ClientAPI, chromadb.Collection]:
    if client is None:
        client = chromadb.PersistentClient(path=settings.chroma_path)
    coll = client.get_or_create_collection(
        name=settings.chroma_collection,
        metadata={"hnsw:space": "cosine"},
    )
    return client, coll


# ---------------------------------------------------------------------------
# Data loading — supports JSONL (scrapper output) and JSON directory
# ---------------------------------------------------------------------------

def _load_jsonl(path: str | Path) -> list[dict]:
    products: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                products.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _log(f"  WARN: skipping malformed line {lineno}: {exc}")
    return products


def _load_json_dir(directory: str | Path) -> list[dict]:
    products: list[dict] = []
    for p in sorted(Path(directory).rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                products.extend(data)
            elif isinstance(data, dict):
                products.append(data)
        except (json.JSONDecodeError, OSError) as exc:
            _log(f"  WARN: skipping {p}: {exc}")
    return products


def load_products(source: str | None = None) -> list[dict]:
    """Load products from JSONL file or JSON directory.

    Resolution order when *source* is None:
      1. ``settings.products_jsonl``  (scrapper output)
      2. ``settings.products_path``   (JSON dir)
    """
    if source:
        p = Path(source)
        if p.is_file():
            return _load_jsonl(p)
        if p.is_dir():
            return _load_json_dir(p)
        raise FileNotFoundError(f"Source not found: {source}")

    jsonl = Path(settings.products_jsonl)
    if jsonl.is_file():
        return _load_jsonl(jsonl)

    json_dir = Path(settings.products_path)
    if json_dir.is_dir():
        return _load_json_dir(json_dir)

    raise FileNotFoundError(
        f"No product data found at {jsonl} or {json_dir}. Run the scrapper first."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _doc_text(product: dict) -> str:
    """Build the text string that gets embedded for a product.

    Includes all available context so that queries like "mortier colle C2"
    or brand-specific searches get high-quality matches.
    """
    parts = [
        product.get("title", ""),
        product.get("brand", ""),
        product.get("source_category", ""),
        product.get("unit", ""),
        " ".join(product.get("category_path") or []),
        product.get("category", ""),
        product.get("subcategory", ""),
        product.get("sub_subcategory", ""),
    ]
    return " ".join(p for p in parts if p)


def _doc_id(product: dict) -> str:
    """Stable unique ID for deduplication inside ChromaDB."""
    return product.get("product_id") or product.get("url") or ""


def _safe_metadata(product: dict) -> dict[str, Any]:
    """ChromaDB metadata values must be str | int | float | bool."""
    keep_keys = [
        "product_id", "sku_id", "title", "price", "rating",
        "review_count", "url", "stock_status", "stock_quantity",
        "source_url", "scrapped_at", "category", "subcategory",
        "sub_subcategory",
    ]
    meta: dict[str, Any] = {}
    for k in keep_keys:
        v = product.get(k)
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            meta[k] = v
        else:
            meta[k] = str(v)
    return meta


# ---------------------------------------------------------------------------
# Build index
# ---------------------------------------------------------------------------

def build_index(source: str | None = None) -> int:
    """Embed products and upsert into ChromaDB.  Returns count indexed."""
    products = load_products(source)
    if not products:
        _log("No products to index.")
        return 0

    # Deduplicate by product_id
    seen: set[str] = set()
    unique: list[dict] = []
    for p in products:
        pid = _doc_id(p)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        unique.append(p)

    _log(f"Loaded {len(products)} rows, {len(unique)} unique products to index.")

    model = _get_model()
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
    _, coll = _get_collection()

    texts = [_doc_text(p) for p in unique]

    _log(f"Encoding {len(texts)} documents (batch={EMBED_BATCH})...")
    t0 = time.perf_counter()
    embeddings = model.encode(
        texts,
        batch_size=EMBED_BATCH,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    elapsed = time.perf_counter() - t0
    _log(f"Encoding done in {elapsed:.1f}s ({len(texts) / max(elapsed, 0.01):.0f} docs/s)")

    ids = [_doc_id(p) for p in unique]
    metadatas = [_safe_metadata(p) for p in unique]

    _log("Upserting into ChromaDB...")
    t0 = time.perf_counter()
    for i in range(0, len(ids), EMBED_BATCH):
        end = min(i + EMBED_BATCH, len(ids))
        coll.upsert(
            ids=ids[i:end],
            documents=texts[i:end],
            metadatas=metadatas[i:end],
            embeddings=embeddings[i:end].tolist(),
        )
    elapsed = time.perf_counter() - t0
    _log(f"Upsert done in {elapsed:.1f}s. Collection size: {coll.count()}")
    return coll.count()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_products(
    query: str,
    top_k: int = 5,
    where: dict | None = None,
) -> list[dict]:
    """Semantic search over indexed products.

    Parameters
    ----------
    query : str
        Free-text query (French or English).
    top_k : int
        Number of results to return.
    where : dict | None
        Optional ChromaDB metadata filter, e.g. ``{"category": "Plomberie"}``.

    Returns
    -------
    list[dict]
        Each dict contains: name, price, unit, category, subcategory,
        sub_subcategory, url, product_id, distance (raw Chroma distance),
        confidence_score (monotonic 1/(1+d)), and full metadata.
    """
    model = _get_model()
    _, coll = _get_collection()

    if coll.count() == 0:
        _log("WARNING: collection is empty — run --build first.")
        return []

    q_embedding = model.encode(
        [query],
        convert_to_numpy=True,
        normalize_embeddings=True,
    )[0].tolist()

    kwargs: dict[str, Any] = {
        "query_embeddings": [q_embedding],
        "n_results": min(top_k, coll.count()),
        "include": ["metadatas", "documents", "distances"],
    }
    if where:
        kwargs["where"] = where

    results = coll.query(**kwargs)

    hits: list[dict] = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        # Stable monotonic mapping: 1/(1+d) ∈ (0, 1] for any d ≥ 0.
        # Works regardless of Chroma distance metric or normalization.
        confidence = 1.0 / (1.0 + float(dist))

        hits.append({
            "name": meta.get("title", doc),
            "price": meta.get("price"),
            "unit": "la pièce",
            "category": meta.get("category", ""),
            "subcategory": meta.get("subcategory", ""),
            "sub_subcategory": meta.get("sub_subcategory", ""),
            "url": meta.get("url", ""),
            "product_id": meta.get("product_id", ""),
            "distance": round(float(dist), 6),
            "confidence_score": round(confidence, 4),
            "metadata": meta,
        })

    return hits


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def collection_stats() -> dict:
    """Return basic stats about the ChromaDB collection."""
    try:
        _, coll = _get_collection()
        count = coll.count()
    except Exception:
        count = 0

    return {
        "collection": settings.chroma_collection,
        "chroma_path": settings.chroma_path,
        "embedding_model": settings.embedding_model,
        "product_count": count,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Vector search over scraped Bricodépôt products."
    )
    ap.add_argument("--build", action="store_true", help="Build / rebuild the index")
    ap.add_argument("--source", default=None, help="Path to JSONL file or JSON directory")
    ap.add_argument("--query", "-q", default=None, help="Search query")
    ap.add_argument("--top-k", "-k", type=int, default=5, help="Number of results (default: 5)")
    ap.add_argument("--category", default=None, help="Filter by category")
    ap.add_argument("--stats", action="store_true", help="Print collection stats")
    args = ap.parse_args()

    if args.stats:
        info = collection_stats()
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return

    if args.build:
        build_index(source=args.source)
        return

    if args.query:
        where = {"category": args.category} if args.category else None
        results = search_products(args.query, top_k=args.top_k, where=where)
        if not results:
            print("No results found.")
            return
        for i, hit in enumerate(results, 1):
            conf = hit["confidence_score"]
            price = hit["price"]
            price_str = f"{price}€" if price is not None else "N/A"
            print(
                f"  {i}. [{conf:.2%}] {hit['name']}  —  {price_str}"
                f"  ({hit['category']} > {hit['subcategory']})"
            )
        print()
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
