#!/usr/bin/env python3
"""
Deterministic task pricing based on public French artisan benchmark ranges.

No LLM is called here.  Same inputs always produce the same output (given
the same feedback DB state).  The formula is:

    hourly_rate  = midpoint of benchmark range for the task category
    base         = hourly_rate × duration_hours × phase_multiplier × regional_modifier
    adjusted     = base + feedback_adjustment   (from feedback.py, 0.0 if none)
    with_margin  = adjusted × (1 + contractor_margin)

CLI (for manual testing):
    python task_pricer.py --category Plumbing --duration "3h" --phase Install --region ile-de-france --margin 0.15
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public benchmark ranges (France) — used as seed priors.
# Sources cited in README.
# ---------------------------------------------------------------------------

LABOR_RATE_RANGES_EUR_PER_HOUR: dict[str, tuple[float, float]] = {
    "Plumbing":    (40, 70),    # Habitatpresto
    "Electrical":  (35, 95),    # Travaux.com
    "Tiling":      (30, 50),    # Ootravaux
    "Painting":    (25, 50),    # Habitatpresto + travauxdepeinture.com
    "Carpentry":   (40, 60),    # prix-travaux-m2.com
    "General":     (35, 45),    # conservative handyman/general fallback
    "default":     (35, 45),
}

PHASE_MULTIPLIERS: dict[str, float] = {
    "Prep":    1.0,
    "Install": 1.25,
    "Finish":  1.1,
}

REGIONAL_MODIFIERS: dict[str, float] = {
    "ile-de-france": 1.15,
    "paris":         1.15,
    "occitanie":     1.00,
    "default":       1.00,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    """Remove diacritics so 'île-de-france' matches 'ile-de-france'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def midpoint_rate(category: str) -> float:
    """Return the midpoint hourly rate for *category*."""
    lo, hi = LABOR_RATE_RANGES_EUR_PER_HOUR.get(
        category,
        LABOR_RATE_RANGES_EUR_PER_HOUR["default"],
    )
    return (lo + hi) / 2.0


def rate_range(category: str) -> tuple[float, float]:
    """Return (lo, hi) benchmark range for *category*."""
    return LABOR_RATE_RANGES_EUR_PER_HOUR.get(
        category,
        LABOR_RATE_RANGES_EUR_PER_HOUR["default"],
    )


def phase_multiplier(phase: str) -> float:
    return PHASE_MULTIPLIERS.get(phase, 1.0)


def regional_modifier(region: str) -> float:
    key = _strip_accents(region).lower().strip() if region else "default"
    return REGIONAL_MODIFIERS.get(key, REGIONAL_MODIFIERS["default"])


# ---------------------------------------------------------------------------
# Duration parser — regex-based, no LLM
# ---------------------------------------------------------------------------

_DURATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "2.5 hours", "2 hours", "2h", "2.5h"
    (re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?|r?s?)?", re.I), "hours"),
    # "half day", "half-day"
    (re.compile(r"half[\s-]?day", re.I), "half_day"),
    # "1 day", "2 days", "N jours", "1 jour"
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:days?|jours?)", re.I), "days"),
    # "1 jour" standalone (without number prefix already captured)
    (re.compile(r"\bjour\b", re.I), "one_day"),
    # bare number (fallback) — interpret as hours
    (re.compile(r"^(\d+(?:\.\d+)?)$"), "bare_number"),
]

DEFAULT_DURATION_HOURS = 8.0


def parse_duration(s: str) -> float:
    """Parse a human-readable duration string into hours.

    Handles English and French forms:
        "2 hours", "3h", "1.5h", "half day", "1 day", "2 jours", "8"
    Falls back to 8.0 hours (1 working day) if unparseable.
    """
    text = (s or "").strip()
    if not text:
        logger.warning("Empty duration string, defaulting to %.1fh", DEFAULT_DURATION_HOURS)
        return DEFAULT_DURATION_HOURS

    for pattern, kind in _DURATION_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue

        if kind == "hours":
            return float(m.group(1))
        if kind == "half_day":
            return 4.0
        if kind == "days":
            return float(m.group(1)) * 8.0
        if kind == "one_day":
            return 8.0
        if kind == "bare_number":
            return float(m.group(1))

    logger.warning("Could not parse duration '%s', defaulting to %.1fh", text, DEFAULT_DURATION_HOURS)
    return DEFAULT_DURATION_HOURS


# ---------------------------------------------------------------------------
# Feedback integration (optional — graceful when feedback.py is not yet built)
# ---------------------------------------------------------------------------

def _compute_feedback_adjustment(task_label: str, base_price: float) -> float:
    """Try to get a feedback-based price adjustment.  Returns 0.0 if the
    feedback module is unavailable or has no matching data."""
    try:
        from feedback import compute_adjustment  # type: ignore[import-not-found]
        return compute_adjustment(task_label, base_price)
    except (ImportError, Exception):
        return 0.0


# ---------------------------------------------------------------------------
# Core pricing function
# ---------------------------------------------------------------------------

def price_task(
    task: dict,
    region: str = "",
    margin: float = 0.0,
) -> dict:
    """Price a single task deterministically.

    Parameters
    ----------
    task : dict
        Must contain at least ``category``, ``duration``, ``phase``.
        Optional: ``label``, ``description``, ``id``, ``quantity``.
    region : str
        Region name (accents and case are normalised internally).
    margin : float
        Contractor margin as a fraction (e.g. 0.15 = 15 %).

    Returns
    -------
    dict
        Priced task with ``base_cost``, ``feedback_adjustment``,
        ``adjusted_cost``, ``with_margin``, ``pricing_method``,
        ``pricing_details``, and the original task fields.
    """
    category = task.get("category", "General")
    duration_raw = str(task.get("duration", ""))
    phase = task.get("phase", "Install")
    label = task.get("label", "")
    quantity = float(task.get("quantity", 1))

    hourly = midpoint_rate(category)
    lo, hi = rate_range(category)
    hours = parse_duration(duration_raw)
    p_mult = phase_multiplier(phase)
    r_mod = regional_modifier(region)

    base = hourly * hours * p_mult * r_mod * quantity
    adjustment = _compute_feedback_adjustment(label, base)
    adjusted = base + adjustment
    with_margin = adjusted * (1.0 + margin)

    details = (
        f"Based on {category} benchmark range "
        f"({lo:.0f}\u2013{hi:.0f} \u20ac/h), "
        f"using midpoint {hourly:.0f} \u20ac/h "
        f"\u00d7 {hours:.1f}h "
        f"\u00d7 {phase} multiplier {p_mult} "
        f"\u00d7 regional modifier {r_mod}"
    )
    if quantity != 1:
        details += f" \u00d7 qty {quantity:.1f}"
    if adjustment:
        details += f" + feedback adjustment {adjustment:+.2f}\u20ac"
    if margin:
        details += f" + margin {margin:.0%}"

    return {
        **{k: task[k] for k in task},
        "hourly_rate": hourly,
        "duration_hours": hours,
        "phase_multiplier": p_mult,
        "regional_modifier": r_mod,
        "base_cost": round(base, 2),
        "feedback_adjustment": round(adjustment, 2),
        "adjusted_cost": round(adjusted, 2),
        "with_margin": round(with_margin, 2),
        "pricing_method": "labor_rate_estimation",
        "pricing_details": details,
    }


def price_tasks(
    tasks: list[dict],
    region: str = "",
    margin: float = 0.0,
) -> list[dict]:
    """Price a list of tasks.  Convenience wrapper around ``price_task``."""
    return [price_task(t, region=region, margin=margin) for t in tasks]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic task pricing (no LLM).")
    ap.add_argument("--category", default="General", help="Task category (default: General)")
    ap.add_argument("--duration", default="1h", help='Duration string, e.g. "2h", "1 day"')
    ap.add_argument("--phase", default="Install", help="Phase: Prep | Install | Finish")
    ap.add_argument("--region", default="", help="Region name (e.g. ile-de-france)")
    ap.add_argument("--margin", type=float, default=0.0, help="Contractor margin fraction (e.g. 0.15)")
    ap.add_argument("--label", default="", help="Task label (used for feedback lookup)")
    ap.add_argument("--quantity", type=float, default=1.0, help="Quantity (default: 1)")
    args = ap.parse_args()

    task = {
        "category": args.category,
        "duration": args.duration,
        "phase": args.phase,
        "label": args.label,
        "quantity": args.quantity,
    }
    result = price_task(task, region=args.region, margin=args.margin)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
