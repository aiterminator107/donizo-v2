#!/usr/bin/env python3
"""
Feedback store and price adjustment engine.

Uses stdlib sqlite3 (no ORM) and difflib for fuzzy label matching.
Time-decayed weighted average of past feedback drives the adjustment.

CLI (for manual testing / inspection):
    python feedback.py --init                           # create table
    python feedback.py --save --label "Mortier colle flexible C2" --actual 18.50 --type too_low
    python feedback.py --adjust --label "Mortier colle flexible C2" --base 15.00
    python feedback.py --list                           # dump all rows
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from config import settings

FUZZY_THRESHOLD = 0.7
DECAY_HALF_LIFE_DAYS = 30.0

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return Path(settings.feedback_db)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the feedback table if it does not exist."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id   TEXT,
            item_type     TEXT,
            item_label    TEXT,
            feedback_type TEXT,
            actual_price  REAL,
            comment       TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_feedback(record: dict) -> int:
    """Insert a feedback row.  Returns the new row id.

    Expected keys (all optional except item_label):
        proposal_id, item_type, item_label, feedback_type, actual_price, comment
    """
    init_db()
    conn = _connect()
    cur = conn.execute(
        """
        INSERT INTO feedback (proposal_id, item_type, item_label,
                              feedback_type, actual_price, comment)
        VALUES (:proposal_id, :item_type, :item_label,
                :feedback_type, :actual_price, :comment)
        """,
        {
            "proposal_id":  record.get("proposal_id", ""),
            "item_type":    record.get("item_type", ""),
            "item_label":   record.get("item_label", ""),
            "feedback_type": record.get("feedback_type", ""),
            "actual_price": record.get("actual_price"),
            "comment":      record.get("comment", ""),
        },
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


# ---------------------------------------------------------------------------
# Read / adjust
# ---------------------------------------------------------------------------

def _fuzzy_ratio(a: str, b: str) -> float:
    """Case-insensitive fuzzy similarity ratio ∈ [0, 1]."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _days_since(iso_str: str) -> float:
    """Return fractional days between *iso_str* (UTC assumed) and now."""
    try:
        created = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0
    delta = datetime.now(timezone.utc) - created
    return max(delta.total_seconds() / 86400.0, 0.0)


def compute_adjustment(item_label: str, base_price: float) -> float:
    """Compute a time-decayed, fuzzy-matched price adjustment.

    1. Fetch all feedback rows that have a non-null ``actual_price``.
    2. Keep rows where ``item_label`` fuzzy-matches with ratio > 0.7.
    3. For each match compute:
       - delta = actual_price - base_price
       - weight = exp(-days_old / 30)
    4. Return weighted average of deltas:  sum(delta_i × w_i) / sum(w_i).
    5. Return 0.0 if no matches found.
    """
    if not item_label:
        return 0.0

    init_db()
    conn = _connect()
    rows = conn.execute(
        "SELECT item_label, actual_price, created_at FROM feedback "
        "WHERE actual_price IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return 0.0

    numerator = 0.0
    denominator = 0.0

    for row in rows:
        ratio = _fuzzy_ratio(item_label, row["item_label"] or "")
        if ratio < FUZZY_THRESHOLD:
            continue

        days_old = _days_since(row["created_at"] or "")
        weight = math.exp(-days_old / DECAY_HALF_LIFE_DAYS)
        delta = float(row["actual_price"]) - base_price

        numerator += delta * weight
        denominator += weight

    if denominator == 0.0:
        return 0.0

    return round(numerator / denominator, 2)


def list_feedback() -> list[dict]:
    """Return all feedback rows as dicts (for debugging / CLI)."""
    init_db()
    conn = _connect()
    rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Feedback store and adjustment engine.")
    ap.add_argument("--init", action="store_true", help="Create the feedback table")
    ap.add_argument("--save", action="store_true", help="Save a feedback record")
    ap.add_argument("--adjust", action="store_true", help="Compute adjustment for a label")
    ap.add_argument("--list", action="store_true", help="List all feedback rows")

    ap.add_argument("--label", default="", help="Item label")
    ap.add_argument("--actual", type=float, default=None, help="Actual price (for --save)")
    ap.add_argument("--base", type=float, default=0.0, help="Base price (for --adjust)")
    ap.add_argument("--type", dest="feedback_type", default="", help="Feedback type (e.g. too_low, too_high)")
    ap.add_argument("--proposal-id", default="", help="Proposal ID")
    ap.add_argument("--item-type", default="task", help="Item type: task or material")
    ap.add_argument("--comment", default="", help="Comment")
    args = ap.parse_args()

    if args.init:
        init_db()
        print(f"Feedback DB initialised at {_db_path()}")
        return

    if args.save:
        row_id = save_feedback({
            "proposal_id": args.proposal_id,
            "item_type": args.item_type,
            "item_label": args.label,
            "feedback_type": args.feedback_type,
            "actual_price": args.actual,
            "comment": args.comment,
        })
        print(f"Saved feedback row id={row_id}")
        return

    if args.adjust:
        adj = compute_adjustment(args.label, args.base)
        print(json.dumps({
            "item_label": args.label,
            "base_price": args.base,
            "adjustment": adj,
            "adjusted_price": round(args.base + adj, 2),
        }, indent=2, ensure_ascii=False))
        return

    if args.list:
        rows = list_feedback()
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
