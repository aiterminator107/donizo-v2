"""REST API routes for the Pricing Engine."""
from __future__ import annotations

from fastapi import APIRouter, Query

from schemas import (
    FeedbackIn,
    HealthResponse,
    MaterialMatch,
    PricedMaterial,
    PricedProposalResponse,
    PricedTask,
    PricingSummary,
    ProposalRequest,
    SearchResult,
)
from search import collection_stats, search_products
from task_pricer import price_task
from feedback import compute_adjustment, init_db, save_feedback

router = APIRouter()

# ---------------------------------------------------------------------------
# POST /price
# ---------------------------------------------------------------------------

@router.post("/price", response_model=PricedProposalResponse)
async def price_proposal(req: ProposalRequest):
    """Price a structured proposal (tasks + materials) deterministically."""
    region = req.metadata.region
    margin = req.contractor_margin

    # ── Price tasks ────────────────────────────────────────────────────
    priced_tasks: list[PricedTask] = []
    total_tasks = 0.0

    for t in req.tasks:
        result = price_task(t.model_dump(), region=region, margin=margin)
        priced_tasks.append(PricedTask(**result))
        total_tasks += result["with_margin"]

    # ── Price materials ────────────────────────────────────────────────
    priced_materials: list[PricedMaterial] = []
    total_materials = 0.0

    for m in req.materials:
        hits = search_products(m.label, top_k=1)
        best = hits[0] if hits else None

        match_obj: MaterialMatch | None = None
        unit_price: float | None = None
        total_price: float | None = None
        adj = 0.0
        adjusted_cost: float | None = None
        with_margin: float | None = None
        conf = 0.0
        method = "not_found"
        details = f"No matching product found for '{m.label}'"

        if best and best.get("price") is not None:
            match_obj = MaterialMatch(
                name=best["name"],
                price=best["price"],
                unit=best.get("unit", ""),
                category=best.get("category", ""),
                url=best.get("url", ""),
                product_id=best.get("product_id", ""),
                distance=best.get("distance", 0.0),
                confidence_score=best.get("confidence_score", 0.0),
            )
            unit_price = float(best["price"])
            total_price = round(unit_price * m.quantity, 2)
            adj = compute_adjustment(m.label, total_price)
            adjusted_cost = round(total_price + adj, 2)
            with_margin = round(adjusted_cost * (1.0 + margin), 2)
            conf = best.get("confidence_score", 0.0)
            method = "semantic_search"
            details = (
                f"Matched '{best['name']}' at {unit_price}€ "
                f"(confidence {conf:.2%}) "
                f"× qty {m.quantity}"
            )
            if adj:
                details += f" + feedback {adj:+.2f}€"
            if margin:
                details += f" + margin {margin:.0%}"

            total_materials += with_margin

        priced_materials.append(PricedMaterial(
            label=m.label,
            unit=m.unit,
            quantity=m.quantity,
            usedIn=m.usedIn,
            match=match_obj,
            unit_price=unit_price,
            total_price=total_price,
            feedback_adjustment=round(adj, 2),
            adjusted_cost=adjusted_cost,
            with_margin=with_margin,
            confidence_score=conf,
            pricing_method=method,
            pricing_details=details,
        ))

    # ── Summary ────────────────────────────────────────────────────────
    total = round(total_tasks + total_materials, 2)

    return PricedProposalResponse(
        title=req.title,
        metadata=req.metadata,
        priced_tasks=priced_tasks,
        priced_materials=priced_materials,
        summary=PricingSummary(
            total_tasks=round(total_tasks, 2),
            total_materials=round(total_materials, 2),
            total=total,
            margin_applied=margin,
        ),
    )


# ---------------------------------------------------------------------------
# POST /feedback
# ---------------------------------------------------------------------------

@router.post("/feedback")
async def submit_feedback(fb: FeedbackIn):
    """Record price feedback for a task or material."""
    row_id = save_feedback(fb.model_dump())
    return {"status": "ok", "id": row_id}


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(default=5, ge=1, le=50),
    category: str | None = Query(default=None),
):
    """Semantic product search."""
    where = {"category": category} if category else None
    hits = search_products(q, top_k=top_k, where=where)
    return [
        SearchResult(
            name=h.get("name", ""),
            price=h.get("price"),
            unit=h.get("unit", ""),
            category=h.get("category", ""),
            subcategory=h.get("subcategory", ""),
            sub_subcategory=h.get("sub_subcategory", ""),
            url=h.get("url", ""),
            product_id=h.get("product_id", ""),
            distance=h.get("distance", 0.0),
            confidence_score=h.get("confidence_score", 0.0),
        )
        for h in hits
    ]


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health():
    """Basic health / readiness check."""
    stats = collection_stats()
    return HealthResponse(
        status="ok",
        chroma_collection_size=stats.get("product_count", 0),
        feedback_db=stats.get("chroma_path", ""),
    )
