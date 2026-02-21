#!/usr/bin/env python3
"""
Orchestrator: reads links.json, flattens the nested category tree,
and invokes fetch_products.py once per leaf URL, passing category metadata.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def flatten_links(data: dict) -> list[tuple[str, str, str, str]]:
    """Flatten nested dict into (category, subcategory, sub_subcategory, path) tuples."""
    entries: list[tuple[str, str, str, str]] = []
    for cat, subcats in data.items():
        for subcat, subsubs in subcats.items():
            for subsub, path in subsubs.items():
                entries.append((cat, subcat, subsub, path))
    return entries


def build_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Orchestrate fetch_products.py across all category links."
    )
    ap.add_argument(
        "--links", default="v2/scrapper/links.json",
        help="Path to links JSON file (default: v2/scrapper/links.json)",
    )
    ap.add_argument(
        "--base-url", default="https://www.bricodepot.fr/",
        help="Base URL to prefix relative paths (default: https://www.bricodepot.fr/)",
    )
    ap.add_argument(
        "--sleep", type=float, default=1.0,
        help="Seconds to sleep between links (default: 1.0)",
    )

    args, extra = ap.parse_known_args()

    links_path = Path(args.links)
    if not links_path.exists():
        log(f"ERROR: links file not found: {links_path}")
        sys.exit(1)

    with open(links_path, encoding="utf-8") as f:
        data = json.load(f)

    entries = flatten_links(data)
    total = len(entries)
    log(f"Loaded {total} links from {links_path}")

    fetch_script = str(Path(__file__).resolve().parent / "fetch_products.py")
    successes = 0
    failures = 0

    for idx, (cat, subcat, subsub, path) in enumerate(entries, 1):
        url = build_url(args.base_url, path)
        log(f"[{idx}/{total}] {cat} > {subcat} > {subsub}")
        log(f"  URL: {url}")

        cmd = [
            sys.executable, fetch_script,
            "--url", url,
            "--category", cat,
            "--subcategory", subcat,
            "--sub-subcategory", subsub,
            *extra,
        ]

        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode != 0:
                log(f"  FAILED (exit code {result.returncode})")
                failures += 1
            else:
                log(f"  OK")
                successes += 1
        except Exception as exc:
            log(f"  EXCEPTION: {exc}")
            failures += 1

        if idx < total:
            time.sleep(args.sleep)

    log(f"Finished: {successes} succeeded, {failures} failed out of {total}")


if __name__ == "__main__":
    main()
