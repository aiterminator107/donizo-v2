#!/usr/bin/env python3
"""
Bricodépôt product list scraper.

Given a category page URL, loads the page, finds all products inside
<div class="bd-ProductsListItem-box"> and extracts title, rating,
review count, price, product URL, and product ID.  Results are written
as JSONL (one JSON object per line).

This version reuses several defensive measures from fetch_links.py:
- persistent profile
- per-run User-Agent rotation and Accept-Language header
- randomized delays
- robust_click helper (normal → force → JS)
- debug HTML dumps when selectors are missing or errors occur

Usage:
    python fetch_products.py --url "https://www.bricodepot.fr/..."
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
DELAY_MIN = 1.0
DELAY_MAX = 3.0
DEFAULT_OUT = str(Path(__file__).resolve().parent / "products.jsonl")


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


async def _dump_page(page: Page, name: str = "page_dump.html") -> None:
    """Write a debug HTML dump of the current page."""
    try:
        html = await page.content()
        dump_path = Path(name)
        dump_path.write_text(html, encoding="utf-8")
        log(f"  📄 Debug dump written to {dump_path.resolve()}")
    except Exception as exc:
        log(f"  ⚠️ Could not write page dump: {exc}")


JS_EXTRACT_PRODUCTS = """
() => {
    function clean(s) { return (s || "").replace(/\\s+/g, " ").trim(); }

    const products = [];
    const items = document.querySelectorAll("div.bd-ProductsListItem-box");

    for (const box of items) {
        const parentItem = box.closest("div.bd-ProductsListItem");

        // Product ID & SKU ID
        const productId = parentItem ? (parentItem.getAttribute("data-product-id") || "") : "";
        const skuId = parentItem ? (parentItem.getAttribute("data-sku-id") || "") : "";
        if (!productId) continue;

        // Title
        const titleEl = box.querySelector("h3.bd-ProductsListItem-title");
        const title = titleEl ? clean(titleEl.getAttribute("data-title") || titleEl.textContent) : "";

        // Price
        const priceEl = box.querySelector("div.bd-Price[data-price]");
        const priceStr = priceEl ? priceEl.getAttribute("data-price") : "";
        const price = priceStr ? parseFloat(priceStr) : null;

        // Product URL
        const linkEl = box.querySelector("div.bd-ProductsListItem-link[data-href]");
        const productUrl = linkEl ? (linkEl.getAttribute("data-href") || "") : "";

        // Rating: extract from bv-off-screen text
        let rating = null;
        let reviewCount = 0;

        const offScreen = box.querySelector("span.bv-off-screen");
        if (offScreen) {
            const offText = clean(offScreen.textContent);
            const ratingMatch = offText.match(/(\\d+\\.?\\d*)\\s+sur\\s+5/);
            if (ratingMatch) {
                rating = parseFloat(ratingMatch[1]);
            }
        }

        // Fallback: rating from CSS class bv-width-from-rating-stats-XX
        if (rating === null) {
            const starsOn = box.querySelector("span.bv-rating-stars-on[class*='bv-width-from-rating-stats-']");
            if (starsOn) {
                const cls = starsOn.className;
                const m = cls.match(/bv-width-from-rating-stats-(\\d+)/);
                if (m) {
                    const pct = parseInt(m[1], 10);
                    rating = pct > 0 ? pct / 20 : null;
                }
            }
        }

        // Review count from "(N)" text
        const ratingLabel = box.querySelector("span.bv-rating-label");
        if (ratingLabel) {
            const countMatch = clean(ratingLabel.textContent).match(/\\((\\d+)\\)/);
            if (countMatch) {
                reviewCount = parseInt(countMatch[1], 10);
            }
        }

        // Stock status
        const stockEl = box.querySelector("div.bd-ProductsListItem-stock");
        const stockStatus = stockEl ? clean(stockEl.getAttribute("data-desc") || stockEl.textContent) : "";
        const stockQty = stockEl ? parseInt(stockEl.getAttribute("data-quantity") || "0", 10) : 0;

        products.push({
            product_id: productId,
            sku_id: skuId,
            title: title,
            price: price,
            rating: rating,
            review_count: reviewCount,
            url: productUrl,
            stock_status: stockStatus,
            stock_quantity: stockQty
        });
    }

    return products;
}
"""


async def extract_products(page: Page, source_url: str) -> list[dict]:
    """Extract all products from the current page."""
    raw = await page.evaluate(JS_EXTRACT_PRODUCTS)
    for item in raw:
        item["source_url"] = source_url
    return raw


async def run(
    url: str,
    out: str,
    headful: bool,
    timeout_ms: int,
    category: str | None = None,
    subcategory: str | None = None,
    sub_subcategory: str | None = None,
) -> list[dict]:
    async with async_playwright() as p:
        log(f"Launching Chromium persistent profile (headless={not headful})...")
        ua = random.choice(USER_AGENTS)
        log(f"Using User-Agent: {ua}")

        context = await p.chromium.launch_persistent_context(
            user_data_dir="bricodepot_profile",
            headless=not headful,
            viewport={"width": 1280, "height": 900},
            locale="fr-FR",
            user_agent=ua,
        )
        try:
            await context.set_extra_http_headers(
                {"User-Agent": ua, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
            )
        except Exception:
            pass

        page = context.pages[0] if context.pages else await context.new_page()

        log(f"Navigating to {url} ...")
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(2000)
        await random_delay()

        log("Waiting for product list to load...")
        try:
            await page.locator("div.bd-List-Content").first.wait_for(
                state="visible", timeout=timeout_ms
            )
        except Exception:
            log("WARNING: div.bd-List-Content not found, trying to extract anyway...")
            await _dump_page(page, name="products_missing_list_dump.html")

        await page.wait_for_timeout(1500)

        all_products: list[dict] = []
        page_num = 1

        while True:
            log(f"Extracting products from page {page_num}...")
            products = await extract_products(page, url)
            log(f"  Found {len(products)} products on page {page_num}")
            all_products.extend(products)

            next_link = page.locator(
                "div.bd-Paging a.bd-Paging-link--next, div.bd-Paging .bd-Icon--sliderRight"
            ).first

            has_next = False
            try:
                if await next_link.count() > 0:
                    parent = next_link.locator("xpath=..")
                    parent_tag = await parent.evaluate("el => el.tagName.toLowerCase()")
                    if parent_tag == "a":
                        has_next = True
                        href = await parent.get_attribute("href")
                        if href:
                            log(f"  Navigating to next page: {href}")
                            ok = await robust_click(parent, f"next-page-{page_num}")
                            if not ok:
                                log("  ⚠️ robust_click failed on next page link, aborting pagination.")
                                break
                            await page.wait_for_timeout(2000)
                            await random_delay()
                            page_num += 1
                            continue
            except Exception:
                # on unexpected error, dump the page for debugging and stop pagination
                log("  ⚠️ Exception during pagination click — dumping page for debug.")
                await _dump_page(page, name=f"pagination_error_page_{page_num}.html")
                break

            if not has_next:
                log("No more pages.")
                break

        log(f"Total products extracted: {len(all_products)}")

        await context.close()
        log("Browser closed.")

    # attach scrapped timestamp (unix seconds) to every product
    scrapped_ts = int(time.time())
    for product in all_products:
        product["scrapped_at"] = scrapped_ts

    # inject optional category metadata if provided
    if category is not None:
        for product in all_products:
            product["category"] = category
    if subcategory is not None:
        for product in all_products:
            product["subcategory"] = subcategory
    if sub_subcategory is not None:
        for product in all_products:
            product["sub_subcategory"] = sub_subcategory

    out_path = Path(out)
    # ensure parent directory exists (handles running from different cwd)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        # best-effort; continue and let open() raise if it still fails
        pass

    log(f"Writing JSONL -> {out} ({len(all_products)} products)")
    with open(out_path, "a", encoding="utf-8") as f:
        for product in all_products:
            f.write(json.dumps(product, ensure_ascii=False) + "\n")

    log("Done.")
    return all_products


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scrape product listings from a Bricodepot category page."
    )
    ap.add_argument(
        "--url", required=True,
        help="Full URL of the category page to scrape"
    )
    ap.add_argument(
        "--out", default=DEFAULT_OUT,
        help=f"Output JSONL file path (default: {DEFAULT_OUT})"
    )
    ap.add_argument("--headful", action="store_true", help="Run in headful mode")
    ap.add_argument(
        "--timeout-ms", type=int, default=60000,
        help="Global timeout in milliseconds (default: 60000)"
    )
    ap.add_argument("--category", default=None, help="Category label")
    ap.add_argument("--subcategory", default=None, help="Subcategory label")
    ap.add_argument("--sub-subcategory", default=None, help="Sub-subcategory label")
    args = ap.parse_args()

    try:
        asyncio.run(run(
            args.url,
            args.out,
            args.headful,
            args.timeout_ms,
            category=args.category,
            subcategory=args.subcategory,
            sub_subcategory=getattr(args, "sub_subcategory"),
        ))
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()

