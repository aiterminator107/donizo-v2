"""FastAPI application entry point for the Pricing Engine."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from feedback import init_db
from routes import router

logger = logging.getLogger("pricing_engine")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init feedback DB and warm up the embedding model."""
    logger.info("Initialising feedback DB...")
    init_db()

    logger.info("Warming up embedding model (first load may download)...")
    try:
        from search import _get_model
        _get_model()
        logger.info("Embedding model ready.")
    except Exception as exc:
        logger.warning("Could not warm up embedding model: %s", exc)

    yield


app = FastAPI(
    title="Pricing Engine v2",
    description=(
        "Deterministic pricing API for construction proposals. "
        "Material prices via semantic search over scraped Bricodépôt data; "
        "labor pricing via benchmark-based hourly rates."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
