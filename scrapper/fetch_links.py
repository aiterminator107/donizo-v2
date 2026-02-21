#!/usr/bin/env python3
"""
Bricodépôt "Produits" menu scraper.

Opens the Produits drawer → selects a category → scrapes the 2nd-level
sub-items and their 3rd-level leaf links from the DOM → writes links.json.

The menu DOM structure (observed Feb 2026):
  ul.bd-Submenu-list.bd-Submenu-list--active   ← active category panel
    li.bd-Submenu-item.hasChild                ← 2nd-level item
      a.bd-Submenu-link > span                 ← label
      ul.bd-Submenu-sublist                    ← 3rd-level container (pre-rendered)
        li.bd-Submenu-item--subitem > a[href]  ← leaf links
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from playwright.async_api import async_playwright, Page
import random

BASE_URL = "https://www.bricodepot.fr"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
DELAY_MIN = 1.0
DELAY_MAX = 3.0
DEFAULT_OUT = str(Path(__file__).resolve().parent / "links.json")


# ── Helpers ────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


async def random_delay() -> None:
    """Simple randomized delay to avoid hammering the server."""
    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


async def robust_click(locator, label: str) -> bool:
    """Try normal click → force click → JS click."""
    try:
        await locator.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass

    for mode in ("normal", "force", "js"):
        try:
            if mode == "normal":
                await locator.click(timeout=5000)
            elif mode == "force":
                await locator.click(timeout=5000, force=True)
            else:
                await locator.evaluate("(el) => el.click()")
            return True
        except Exception:
            continue

    log(f"  ⚠️ Could not click '{label}'.")
    return False


# ── Navigation ─────────────────────────────────────────────────────────────

async def open_produits(page: Page, timeout_ms: int) -> None:
    """Open the Produits drawer from the header."""
    log("Opening 'Produits' menu…")
    btn = page.locator(
        "button.jsbd-gtm-toggleMenu:has-text('Produits'), button:has-text('Produits')"
    ).first
    if not await btn.count():
        raise RuntimeError("Could not find 'Produits' button.")

    ok = await robust_click(btn, "Produits")
    if not ok:
        raise RuntimeError("Failed to click 'Produits' button.")

    await page.locator(
        "ul.bd-MenuLink-list > li.bd-MenuLink-item"
    ).first.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(1200)
    log("'Produits' drawer visible.")


async def ensure_main_level(page: Page, timeout_ms: int) -> None:
    """
    Persistent profile may reopen the drawer at a deep submenu.
    Click "Retour" (which is an <a>, not <button>) until we reach top-level.
    """
    log("Ensuring we are at main (level-1) menu…")

    for _ in range(10):
        # "Retour" is <a class="bd-mobileSubMenu-backButton">
        retour = page.locator("a.bd-mobileSubMenu-backButton").first
        try:
            if await retour.count() and await retour.is_visible():
                await robust_click(retour, "Retour")
                await page.wait_for_timeout(400)
                continue
        except Exception:
            pass
        break

    await page.locator(
        "ul.bd-MenuLink-list > li.bd-MenuLink-item"
    ).first.wait_for(state="visible", timeout=timeout_ms)
    log("Main menu level ready.")


async def click_main_category(page: Page, category: str, timeout_ms: int) -> None:
    """Click a top-level category (e.g. Plomberie) in the main menu."""
    log(f"Selecting main category: {category}")

    cat_re = re.compile(rf"^{re.escape(category)}$", re.IGNORECASE)

    # Strategy 1: displayName labels
    candidates = page.locator(
        "ul.bd-MenuLink-list li.bd-MenuLink-item .bd-MenuLink-displayName"
    ).filter(has_text=cat_re)
    n = await candidates.count()
    log(f"  displayName candidates: {n}")

    # Strategy 2: parent li items (broader)
    li_candidates = page.locator(
        "ul.bd-MenuLink-list > li.bd-MenuLink-item"
    ).filter(has_text=cat_re)
    n_li = await li_candidates.count()
    log(f"  li candidates: {n_li}")

    if n == 0 and n_li == 0:
        names = await page.locator(
            "ul.bd-MenuLink-list > li.bd-MenuLink-item"
        ).all_inner_texts()
        names = [clean(x) for x in names if clean(x)]
        raise RuntimeError(
            f"Main category {category!r} not found. Visible: {names[:20]}"
        )

    # Try to get a bounding box — displayName, then li, then a/button inside li.
    chosen_box = None

    for i in range(n):
        el = candidates.nth(i)
        box = await el.bounding_box()
        if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
            chosen_box = box
            log(f"  Found box on displayName[{i}]")
            break

    if not chosen_box:
        for i in range(n_li):
            el = li_candidates.nth(i)
            box = await el.bounding_box()
            if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
                chosen_box = box
                log(f"  Found box on li[{i}]")
                break

    if not chosen_box:
        for i in range(n_li):
            btn = li_candidates.nth(i).locator(":scope > a, :scope > button").first
            if await btn.count():
                box = await btn.bounding_box()
                if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
                    chosen_box = box
                    log(f"  Found box on li[{i}] > a/button")
                    break

    # JS fallback
    if not chosen_box:
        log("  No bounding box found, trying JS click fallback…")
        clicked = await page.evaluate("""
            (cat) => {
                const items = document.querySelectorAll(
                    "ul.bd-MenuLink-list li.bd-MenuLink-item"
                );
                for (const li of items) {
                    const txt = (li.textContent || "").replace(/\\s+/g, " ").trim();
                    if (txt.toLowerCase().includes(cat.toLowerCase())) {
                        const clickable = li.querySelector("a, button") || li;
                        clickable.click();
                        return true;
                    }
                }
                return false;
            }
        """, category)
        if not clicked:
            raise RuntimeError(
                f"Could not click {category!r} — all strategies failed."
            )
        log("  Clicked via JS fallback.")
    else:
        x = chosen_box["x"] + chosen_box["width"] / 2
        y = chosen_box["y"] + chosen_box["height"] / 2
        log(f"  Clicking via mouse at ({x:.0f}, {y:.0f})")
        await page.mouse.click(x, y)

    # Wait for the active category panel to appear in DOM
    await page.locator("ul.bd-Submenu-list--active").first.wait_for(
        state="attached", timeout=timeout_ms
    )
    await page.wait_for_timeout(800)
    log("Category panel opened.")


# ── Extraction ─────────────────────────────────────────────────────────────

JS_EXTRACT_TREE = """
(category) => {
    function clean(s) { return (s || "").replace(/\\s+/g, " ").trim(); }

    // The active category panel has class "bd-Submenu-list--active"
    const panel = document.querySelector("ul.bd-Submenu-list--active");
    if (!panel) return { _error: "No ul.bd-Submenu-list--active found" };

    const result = {};

    // 2nd-level items: direct children <li> that have class "hasChild"
    // and are NOT back-items, "see all" items, or the last empty item.
    const items = panel.querySelectorAll(
        ":scope > li.bd-Submenu-item.hasChild:not(.bd-seeAllProducts):not(.bd-Submenu-item--backItem)"
    );

    for (const li of items) {
        // Label from the direct <a> > <span>
        const linkEl = li.querySelector(":scope > a.bd-Submenu-link");
        if (!linkEl) continue;

        const spanEl = linkEl.querySelector("span");
        const label = clean(spanEl ? spanEl.textContent : linkEl.textContent);
        if (!label) continue;

        // 3rd-level leaf links from nested ul.bd-Submenu-sublist
        const childUl = li.querySelector("ul.bd-Submenu-sublist");
        const leaves = {};

        if (childUl) {
            // Only pick li.bd-Submenu-item--subitem (actual leaf items)
            const leafItems = childUl.querySelectorAll(
                "li.bd-Submenu-item--subitem > a.bd-Submenu-link[href]"
            );
            for (const a of leafItems) {
                const aSpan = a.querySelector("span");
                const text = clean(aSpan ? aSpan.textContent : a.textContent);
                if (!text) continue;

                let href = a.getAttribute("href") || "";
                try {
                    const u = new URL(href, location.origin);
                    href = u.pathname + (u.search || "") + (u.hash || "");
                } catch (e) {}
                if (href && !href.endsWith("/")) href += "/";

                leaves[text] = href;
            }
        }

        result[label] = leaves;
    }

    return { [category]: result };
}
"""

JS_DUMP_ACTIVE_PANEL = """
() => {
    const panel = document.querySelector("ul.bd-Submenu-list--active");
    return panel ? panel.outerHTML : "<no active panel found>";
}
"""


async def extract_category_tree(page: Page, category: str) -> dict:
    """
    Extract the full 2nd/3rd level tree from the currently active
    category panel. All data is pre-rendered in DOM — no clicking needed.
    """
    data = await page.evaluate(JS_EXTRACT_TREE, category)

    # Check for errors
    if "_error" in data:
        log(f"  ⚠️ Extraction error: {data['_error']}")
        await _dump_panel(page)
        return {category: {}}

    # Validate
    tree = data.get(category, {})
    total_l2 = len(tree)
    total_l3 = sum(len(v) for v in tree.values())

    if total_l2 == 0:
        log("  ⚠️ No 2nd-level items found — dumping panel for debug.")
        await _dump_panel(page)

    if total_l3 == 0 and total_l2 > 0:
        log("  ⚠️ Found 2nd-level items but zero 3rd-level links — dumping panel.")
        await _dump_panel(page)

    for label, leaves in tree.items():
        log(f"    {label}: {len(leaves)} links")

    return data


async def _dump_panel(page: Page) -> None:
    """Write panel_dump.html for debugging."""
    try:
        html = await page.evaluate(JS_DUMP_ACTIVE_PANEL)
        dump_path = Path("panel_dump.html")
        dump_path.write_text(html, encoding="utf-8")
        log(f"  📄 Debug dump written to {dump_path.resolve()}")
    except Exception as exc:
        log(f"  ⚠️ Could not write panel dump: {exc}")


# ── Main runner ────────────────────────────────────────────────────────────

async def run(category: str, out: str, headful: bool, timeout_ms: int) -> dict:
    async with async_playwright() as p:
        log(f"Launching Chromium persistent profile (headless={not headful})…")
        ua = random.choice(USER_AGENTS)
        log(f"Using User-Agent: {ua}")
        context = await p.chromium.launch_persistent_context(
            user_data_dir="bricodepot_profile",
            headless=not headful,
            viewport={"width": 450, "height": 900},
            locale="fr-FR",
            user_agent=ua,
        )
        # Also set it as an extra header for any navigation requests
        try:
            await context.set_extra_http_headers(
                {"User-Agent": ua, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
            )
        except Exception:
            # set_extra_http_headers may fail on some profiles; not fatal
            pass

        page = context.pages[0] if context.pages else await context.new_page()

        log(f"Navigating to {BASE_URL}/ …")
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1200)
        await random_delay()

        await open_produits(page, timeout_ms)
        await random_delay()
        await ensure_main_level(page, timeout_ms)
        await random_delay()
        await click_main_category(page, category, timeout_ms)
        await random_delay()

        log("Extracting category tree from DOM…")
        data = await extract_category_tree(page, category)

        # Summary
        tree = data.get(category, {})
        total_l2 = len(tree)
        total_l3 = sum(len(v) for v in tree.values())
        log(f"✅ Extracted {total_l2} sub-categories, {total_l3} total leaf links.")

        await context.close()
        log("Browser closed.")

    # Merge into existing file (append mode)
    out_path = Path(out)
    # ensure parent dir exists (handles running from different cwd)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            log(f"Loaded existing {out} with {len(existing)} categor(ies).")
        except (json.JSONDecodeError, OSError):
            log(f"⚠️ Could not parse existing {out}, starting fresh.")

    existing.update(data)

    log(f"Writing JSON → {out_path} ({len(existing)} total categor(ies))")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    log("Done.")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scrape Bricodépôt Produits menu tree for a category."
    )
    ap.add_argument(
        "--category", default="Plomberie",
        help="Main menu category to open (default: Plomberie)"
    )
    ap.add_argument(
        "--out", default=DEFAULT_OUT,
        help=f"Output JSON file path (default: {DEFAULT_OUT})"
    )
    ap.add_argument("--headful", action="store_true", help="Run in headful mode")
    ap.add_argument(
        "--timeout-ms", type=int, default=60000,
        help="Global timeout in milliseconds (default: 60000)"
    )
    args = ap.parse_args()

    try:
        asyncio.run(run(clean(args.category), args.out, args.headful, args.timeout_ms))
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
