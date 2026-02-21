"""Pydantic models for the Pricing Engine REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TaskIn(BaseModel):
    id: str = ""
    label: str
    description: str = ""
    category: str = "General"
    zone: str = ""
    phase: str = "Install"
    unit: str = ""
    quantity: float = 1.0
    duration: str = "1h"


class MaterialIn(BaseModel):
    label: str
    unit: str = ""
    quantity: float = 1.0
    usedIn: list[str] = Field(default_factory=list)


class ProposalMetadata(BaseModel):
    city: str = ""
    region: str = ""
    jobType: str = ""
    language: str = ""


class ProposalRequest(BaseModel):
    title: str = ""
    metadata: ProposalMetadata = Field(default_factory=ProposalMetadata)
    tasks: list[TaskIn] = Field(default_factory=list)
    materials: list[MaterialIn] = Field(default_factory=list)
    contractor_margin: float = Field(default=0.0, ge=0.0, le=1.0)


class FeedbackIn(BaseModel):
    proposal_id: str = ""
    item_type: str = "task"
    item_label: str
    feedback_type: str = ""
    actual_price: float
    comment: str = ""


class SearchQuery(BaseModel):
    q: str
    top_k: int = Field(default=5, ge=1, le=50)
    category: str | None = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PricedTask(BaseModel):
    id: str = ""
    label: str = ""
    description: str = ""
    category: str = ""
    zone: str = ""
    phase: str = ""
    unit: str = ""
    quantity: float = 1.0
    duration: str = ""
    hourly_rate: float = 0.0
    duration_hours: float = 0.0
    phase_multiplier: float = 1.0
    regional_modifier: float = 1.0
    base_cost: float = 0.0
    feedback_adjustment: float = 0.0
    adjusted_cost: float = 0.0
    with_margin: float = 0.0
    pricing_method: str = ""
    pricing_details: str = ""


class MaterialMatch(BaseModel):
    name: str = ""
    price: float | None = None
    unit: str = ""
    category: str = ""
    url: str = ""
    product_id: str = ""
    distance: float = 0.0
    confidence_score: float = 0.0


class PricedMaterial(BaseModel):
    label: str = ""
    unit: str = ""
    quantity: float = 1.0
    usedIn: list[str] = Field(default_factory=list)
    match: MaterialMatch | None = None
    unit_price: float | None = None
    total_price: float | None = None
    feedback_adjustment: float = 0.0
    adjusted_cost: float | None = None
    with_margin: float | None = None
    confidence_score: float = 0.0
    pricing_method: str = ""
    pricing_details: str = ""


class PricingSummary(BaseModel):
    total_tasks: float = 0.0
    total_materials: float = 0.0
    total: float = 0.0
    margin_applied: float = 0.0
    currency: str = "EUR"


class PricedProposalResponse(BaseModel):
    title: str = ""
    metadata: ProposalMetadata = Field(default_factory=ProposalMetadata)
    priced_tasks: list[PricedTask] = Field(default_factory=list)
    priced_materials: list[PricedMaterial] = Field(default_factory=list)
    summary: PricingSummary = Field(default_factory=PricingSummary)


class SearchResult(BaseModel):
    name: str = ""
    price: float | None = None
    unit: str = ""
    category: str = ""
    subcategory: str = ""
    sub_subcategory: str = ""
    url: str = ""
    product_id: str = ""
    distance: float = 0.0
    confidence_score: float = 0.0


class HealthResponse(BaseModel):
    status: str = "ok"
    chroma_collection_size: int = 0
    feedback_db: str = ""
