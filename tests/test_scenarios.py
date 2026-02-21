"""
Integration tests for the Pricing Engine v2 REST API.

These tests use FastAPI's TestClient (no running server needed).
They exercise the full pipeline: search → pricing → feedback loop.

Run from v2/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WATER_HEATER_PROPOSAL = {
    "title": "Water heater installation",
    "metadata": {"city": "Paris", "region": "ile-de-france", "jobType": "renovation"},
    "tasks": [
        {
            "id": "t1",
            "label": "Remove old water heater",
            "category": "Plumbing",
            "phase": "Prep",
            "duration": "1h",
        },
        {
            "id": "t2",
            "label": "Install new 200L water heater",
            "category": "Plumbing",
            "phase": "Install",
            "duration": "3h",
        },
    ],
    "materials": [
        {"label": "Chauffe-eau electrique 200L", "quantity": 1},
        {"label": "Groupe de securite chauffe-eau", "quantity": 1},
    ],
    "contractor_margin": 0.15,
}

BATHROOM_RENO_PROPOSAL = {
    "title": "Bathroom renovation",
    "metadata": {"city": "Toulouse", "region": "occitanie", "jobType": "renovation"},
    "tasks": [
        {
            "id": "t1",
            "label": "Prepare bathroom floor",
            "category": "Tiling",
            "phase": "Prep",
            "duration": "2h",
        },
        {
            "id": "t2",
            "label": "Install floor tiles",
            "category": "Tiling",
            "phase": "Install",
            "duration": "4h",
        },
    ],
    "materials": [
        {"label": "Mortier colle flexible C2", "quantity": 2},
        {"label": "Joint carrelage gris", "quantity": 1},
    ],
    "contractor_margin": 0.10,
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert isinstance(data["chroma_collection_size"], int)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_results(self):
        r = client.get("/search", params={"q": "chauffe-eau", "top_k": 3})
        assert r.status_code == 200
        results = r.json()
        assert isinstance(results, list)
        assert len(results) > 0
        hit = results[0]
        assert "name" in hit
        assert "price" in hit
        assert "confidence_score" in hit
        assert "distance" in hit
        assert hit["confidence_score"] > 0

    def test_search_with_category_filter(self):
        r = client.get("/search", params={"q": "carrelage", "top_k": 3, "category": "Carrelage - Stratifié & Parquet"})
        assert r.status_code == 200
        results = r.json()
        for hit in results:
            assert hit["category"] == "Carrelage - Stratifié & Parquet"

    def test_search_empty_query_rejected(self):
        r = client.get("/search", params={"q": "", "top_k": 3})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Scenario 1: Water Heater pricing
# ---------------------------------------------------------------------------

class TestWaterHeaterPricing:
    def test_price_returns_priced_tasks_and_materials(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        assert r.status_code == 200
        data = r.json()

        assert "priced_tasks" in data
        assert "priced_materials" in data
        assert "summary" in data

        assert len(data["priced_tasks"]) == 2
        assert len(data["priced_materials"]) == 2

    def test_summary_total_is_positive(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        data = r.json()
        summary = data["summary"]

        assert summary["total"] > 0
        assert summary["total_tasks"] > 0
        assert summary["total_materials"] > 0
        assert summary["margin_applied"] == 0.15

    def test_priced_tasks_have_required_fields(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        data = r.json()

        for task in data["priced_tasks"]:
            assert task["pricing_method"] == "labor_rate_estimation"
            assert task["base_cost"] > 0
            assert task["with_margin"] >= task["adjusted_cost"]
            assert "pricing_details" in task
            assert task["hourly_rate"] > 0
            assert task["duration_hours"] > 0

    def test_priced_materials_have_confidence_and_margin(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        data = r.json()

        for mat in data["priced_materials"]:
            if mat["match"] is not None:
                assert mat["confidence_score"] > 0
                assert mat["with_margin"] is not None
                assert mat["with_margin"] >= (mat["total_price"] or 0)

    def test_regional_modifier_applied(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        data = r.json()

        for task in data["priced_tasks"]:
            assert task["regional_modifier"] == 1.15

    def test_total_equals_tasks_plus_materials(self):
        r = client.post("/price", json=WATER_HEATER_PROPOSAL)
        data = r.json()
        summary = data["summary"]
        assert abs(summary["total"] - (summary["total_tasks"] + summary["total_materials"])) < 0.01


# ---------------------------------------------------------------------------
# Scenario 2: Feedback flow
# ---------------------------------------------------------------------------

class TestFeedbackFlow:
    def test_feedback_adjusts_future_pricing(self):
        r1 = client.post("/price", json=BATHROOM_RENO_PROPOSAL)
        assert r1.status_code == 200
        data1 = r1.json()

        mortier_1 = next(
            (m for m in data1["priced_materials"] if "Mortier" in m["label"]),
            None,
        )
        assert mortier_1 is not None
        cost_before = mortier_1["adjusted_cost"]

        fb_resp = client.post("/feedback", json={
            "item_label": "Mortier colle flexible C2",
            "actual_price": (cost_before or 0) + 5.0,
            "feedback_type": "too_low",
            "item_type": "material",
        })
        assert fb_resp.status_code == 200
        assert fb_resp.json()["status"] == "ok"

        r2 = client.post("/price", json=BATHROOM_RENO_PROPOSAL)
        assert r2.status_code == 200
        data2 = r2.json()

        mortier_2 = next(
            (m for m in data2["priced_materials"] if "Mortier" in m["label"]),
            None,
        )
        assert mortier_2 is not None
        cost_after = mortier_2["adjusted_cost"]

        if cost_before is not None and cost_after is not None:
            assert cost_after > cost_before, (
                f"Expected adjusted_cost to increase after 'too_low' feedback: "
                f"before={cost_before}, after={cost_after}"
            )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_proposal(self):
        r = client.post("/price", json={
            "title": "Empty",
            "metadata": {},
            "tasks": [],
            "materials": [],
            "contractor_margin": 0.0,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["total"] == 0

    def test_unknown_material_returns_not_found(self):
        r = client.post("/price", json={
            "title": "Unknown material test",
            "metadata": {},
            "tasks": [],
            "materials": [{"label": "xyznonexistent999", "quantity": 1}],
            "contractor_margin": 0.0,
        })
        assert r.status_code == 200
        data = r.json()
        mat = data["priced_materials"][0]
        assert mat["with_margin"] is not None or mat["pricing_method"] == "not_found"

    def test_feedback_validation(self):
        r = client.post("/feedback", json={
            "item_label": "Test item",
            "actual_price": 10.0,
        })
        assert r.status_code == 200

    def test_zero_margin(self):
        proposal = {**WATER_HEATER_PROPOSAL, "contractor_margin": 0.0}
        r = client.post("/price", json=proposal)
        assert r.status_code == 200
        data = r.json()
        for task in data["priced_tasks"]:
            assert abs(task["with_margin"] - task["adjusted_cost"]) < 0.01
