# Pricing Engine v2

Deterministic pricing API for construction proposals.  Material prices via
semantic search over scraped Bricodépôt data; labor pricing via
benchmark-based hourly rates.  No LLM is called during pricing.

## Quickstart

```bash
# 1. Install dependencies
cd v2
python -m pip install -r requirements.txt
python -m playwright install chromium

# 2. Prepare persistent Playwright profile (recommended)
The scraper uses a persistent Chromium profile (`v2/bricodepot_profile`) to
store cookies, localStorage and cached resources which makes repeated scraping
more reliable and faster. The profile is created automatically on first run,
but you can pre-create and inspect it (recommended when debugging or to accept
site cookies) with a headful run:

```bash
# Run a single fetch in headful mode to create and inspect the profile
python scrapper/fetch_products.py --url "https://www.bricodepot.fr/catalogue/..." --out scrapper/products.jsonl --headful
```

When the browser opens for the first time, please accept the site's cookie
banner (\"Accepter\" / \"Tout accepter\") in the page UI. The consent is stored in
`v2/bricodepot_profile` so subsequent headless runs reuse the same cookie state
and behave consistently.

Notes:
- The profile folder is `v2/bricodepot_profile`. It is ignored by git via
  `.gitignore` and may contain sensitive cookies — do not commit it.
- To reset state, delete the folder:

```bash
rm -rf v2/bricodepot_profile   # or delete via Explorer on Windows
```

# 3. Discover links and scrape products
# First, extract the category → leaf listing links that the orchestrator will use:
python scrapper/fetch_links.py

# Then run the orchestrator which consumes the generated links.json:
python scrapper/scrapper.py --links scrapper/links.json

# 3. Build the vector index (or let scrapper do it automatically)
python search.py --build --source scrapper/products.jsonl

# 4. Start the API server
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 5. Run tests
python -m pytest tests/ -v
```

## Pipeline flow

```
Contractor proposal (JSON)
        │
        ▼
   POST /price ──────────────────────────────────────────┐
        │                                                │
        ├── For each task:                               │
        │     task_pricer.py                             │
        │       midpoint(benchmark range)                │
        │       × duration × phase × region              │
        │       + feedback adjustment                    │
        │       × (1 + margin)                           │
        │                                                │
        ├── For each material:                           │
        │     search.py → ChromaDB (semantic search)     │
        │       best match price                         │
        │       × quantity                               │
        │       + feedback adjustment                    │
        │       × (1 + margin)                           │
        │                                                │
        └── Summary: total_tasks + total_materials ──────┘
                                                         │
        POST /feedback  ◄────────────────────────────────┘
           (contractor corrects prices → adjusts future runs)
```

## File layout

```
v2/
├── main.py             # FastAPI app entry point + lifespan
├── routes.py           # 4 REST endpoints
├── schemas.py          # Pydantic request/response models
├── search.py           # ChromaDB vector index + semantic query
├── task_pricer.py      # Deterministic labor pricing (benchmark ranges)
├── feedback.py         # SQLite feedback store + time-decayed adjustment
├── config.py           # pydantic-settings (.env loading)
├── scrapper/           # Playwright scraper (see scrapper/README.md)
│   ├── fetch_links.py
│   ├── fetch_products.py
│   ├── scrapper.py     # Orchestrator
│   ├── links.json
│   └── products.jsonl
├── data/
│   ├── chroma/         # ChromaDB persisted index
│   └── feedback.db     # SQLite feedback DB
├── tests/
│   └── test_scenarios.py  # 15 integration tests
├── requirements.txt
└── README.md
```

## REST API

Start: `python -m uvicorn main:app --port 8000`

Interactive docs: http://localhost:8000/docs (Swagger UI)

| Method | Path       | Description                                      |
|--------|------------|--------------------------------------------------|
| POST   | /price     | Price a structured proposal (tasks + materials)   |
| POST   | /feedback  | Submit price feedback for a task or material      |
| GET    | /search    | Semantic product search (`?q=&top_k=&category=`)  |
| GET    | /health    | Health check (collection size, status)            |

### POST /price — example

```bash
curl -X POST http://localhost:8000/price \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Water heater install",
    "metadata": {"region": "ile-de-france"},
    "tasks": [
      {"label": "Install water heater", "category": "Plumbing", "phase": "Install", "duration": "3h"}
    ],
    "materials": [
      {"label": "Chauffe-eau electrique 200L", "quantity": 1}
    ],
    "contractor_margin": 0.15
  }'
```

Response includes `priced_tasks`, `priced_materials`, and a `summary` with totals.

## Semantic search (`search.py`)

Indexes scraped products into ChromaDB using `paraphrase-multilingual-MiniLM-L12-v2`
embeddings.  Handles French and English queries natively — no LLM at search time.

- Embedding text per product:
  `"{title} {brand} {source_category} {unit} {category_path} {category} {subcategory} {sub_subcategory}"`
- Confidence: `1 / (1 + distance)` — monotonic, bounded (0,1], robust across
  Chroma distance metrics.  Raw distance also returned.
- CLI: `python search.py --query "chauffe-eau 200L" -k 5`
- Build: `python search.py --build --source scrapper/products.jsonl`
- Stats: `python search.py --stats`

## Labor pricing (`task_pricer.py`)

Fully deterministic — no LLM.  Hourly rates seeded from public French artisan
benchmark ranges, refined over time by the feedback loop.

| Category    | Range (€/h) | Midpoint | Source                                |
|-------------|-------------|----------|---------------------------------------|
| Plumbing    | 40 – 70     | 55       | Habitatpresto                         |
| Electrical  | 35 – 95     | 65       | Travaux.com                           |
| Tiling      | 30 – 50     | 40       | Ootravaux                             |
| Painting    | 25 – 50     | 37.5     | Habitatpresto / travauxdepeinture.com |
| Carpentry   | 40 – 60     | 50       | prix-travaux-m2.com                   |
| General     | 35 – 45     | 40       | conservative fallback                 |

Formula:

```
base        = midpoint_rate × duration_hours × phase_multiplier × regional_modifier
adjusted    = base + feedback_adjustment
with_margin = adjusted × (1 + contractor_margin)
```

Phase multipliers: Prep ×1.0, Install ×1.25, Finish ×1.1.
Regional modifiers: Île-de-France ×1.15; default ×1.00.

Billed artisan rates exceed raw wage grids (CAPEB, INSEE) because they include
business overhead, insurance, travel, and margin.

CLI: `python task_pricer.py --category Plumbing --duration "3h" --phase Install --region ile-de-france --margin 0.15`

## Feedback loop (`feedback.py`)

SQLite-backed feedback store with time-decayed price adjustment.

- `save_feedback(record)` — inserts a feedback row (proposal_id, item_label,
  actual_price, feedback_type, comment).
- `compute_adjustment(item_label, base_price)` — fuzzy-matches past feedback
  (difflib ratio > 0.7), applies time decay `exp(-days_old / 30)`, returns
  weighted average delta.  Returns 0.0 if no matches.
- `task_pricer.py` and `/price` call this automatically.

CLI: `python feedback.py --save --label "Mortier colle C2" --actual 18.50 --type too_low`

## Testing APIs + Feedback (example run)

The following sequence demonstrates how feedback changes pricing. Paste the JSON bodies into Postman (or use curl) against `http://localhost:8000`.

1) Price (task-only) — before feedback

POST http://localhost:8000/price
Request body:

```json
{
  "title": "Task test — before feedback",
  "metadata": { "region": "ile-de-france" },
  "tasks": [
    {
      "label": "Mortier colle flexible C2",
      "category": "Plumbing",
      "phase": "Install",
      "duration": "3h",
      "quantity": 1
    }
  ],
  "materials": [],
  "contractor_margin": 0.15
}
```

Response:

```json
{
  "title": "Task test — before feedback",
  "metadata": {
    "city": "",
    "region": "ile-de-france",
    "jobType": "",
    "language": ""
  },
  "priced_tasks": [
    {
      "id": "",
      "label": "Mortier colle flexible C2",
      "description": "",
      "category": "Plumbing",
      "zone": "",
      "phase": "Install",
      "unit": "",
      "quantity": 1.0,
      "duration": "3h",
      "hourly_rate": 55.0,
      "duration_hours": 3.0,
      "phase_multiplier": 1.25,
      "regional_modifier": 1.15,
      "base_cost": 237.19,
      "feedback_adjustment": -216.19,
      "adjusted_cost": 21.0,
      "with_margin": 24.15,
      "pricing_method": "labor_rate_estimation",
      "pricing_details": "Based on Plumbing benchmark range (40–70 €/h), using midpoint 55 €/h × 3.0h × Install multiplier 1.25 × regional modifier 1.15 + feedback adjustment -216.19€ + margin 15%"
    }
  ],
  "priced_materials": [],
  "summary": {
    "total_tasks": 24.15,
    "total_materials": 0.0,
    "total": 24.15,
    "margin_applied": 0.15,
    "currency": "EUR"
  }
}
```

2) Submit feedback

POST http://localhost:8000/feedback
Request body:

```json
{
  "proposal_id": "test-task-1",
  "item_type": "task",
  "item_label": "Mortier colle flexible C2",
  "feedback_type": "too_low",
  "actual_price": 18.5,
  "comment": "Observed retail price"
}
```

Response:

```json
{
  "status": "ok",
  "id": 6
}
```

3) Price — same proposal after feedback

Repeat POST http://localhost:8000/price with the same request body as step (1).

Response:

```json
{
  "title": "Task test — before feedback",
  "metadata": {
    "city": "",
    "region": "ile-de-france",
    "jobType": "",
    "language": ""
  },
  "priced_tasks": [
    {
      "id": "",
      "label": "Mortier colle flexible C2",
      "description": "",
      "category": "Plumbing",
      "zone": "",
      "phase": "Install",
      "unit": "",
      "quantity": 1.0,
      "duration": "3h",
      "hourly_rate": 55.0,
      "duration_hours": 3.0,
      "phase_multiplier": 1.25,
      "regional_modifier": 1.15,
      "base_cost": 237.19,
      "feedback_adjustment": -217.04,
      "adjusted_cost": 20.15,
      "with_margin": 23.17,
      "pricing_method": "labor_rate_estimation",
      "pricing_details": "Based on Plumbing benchmark range (40–70 €/h), using midpoint 55 €/h × 3.0h × Install multiplier 1.25 × regional modifier 1.15 + feedback adjustment -217.04€ + margin 15%"
    }
  ],
  "priced_materials": [],
  "summary": {
    "total_tasks": 23.17,
    "total_materials": 0.0,
    "total": 23.17,
    "margin_applied": 0.15,
    "currency": "EUR"
  }
}
```

Notes:
- `feedback_adjustment` is the time‑decayed weighted average of (actual_price − base_price) across fuzzy‑matched feedback rows; it is applied additively to `base_cost`.
- `adjusted_cost = base_cost + feedback_adjustment`
- `with_margin = adjusted_cost × (1 + contractor_margin)`

## Quick architecture summary

Simple ASCII diagram (high level)

```
   +-------------+       +----------------+       +---------------+
   |  Client /   |  -->  |  FastAPI API   |  -->  |  Task pricer  |
   |  Frontend   |       |  (routes.py)   |       | (task_pricer) |
   +-------------+       +----------------+       +---------------+
                               |
                               | (materials search)
                               v
                         +-------------+
                         | ChromaDB /  |  <-- populated by scraper
                         | embeddings  |
                         +-------------+
                               |
                               v
                         +-------------+
                         | Scraper     |
                         | (Playwright)|
                         +-------------+

  Feedback storage (SQLite) <- POST /feedback (feedback.py) -> influences pricing adjustments
```

## Environment variables (.env)

The project reads configuration from `v2/.env` (via `v2/config.py`). Keys present in the repo `.env`:

- GEMINI_API_KEY (optional) — placeholder for future Gemini extractor (not used in core pricing)
- GEMINI_MODEL (optional) — placeholder model name (not used)
- CHROMA_PATH — path where ChromaDB persists vectors (default: `data/chroma`)
- PRODUCTS_PATH — path for scraped product JSON files (default: `data/products`)
- FEEDBACK_DB — sqlite path for feedback DB (default: `data/feedback.db`)
- SCRAPE_DELAY_MIN — minimum randomized delay between scraper actions (float)
- SCRAPE_DELAY_MAX — maximum randomized delay between scraper actions (float)

Optional runtime env:
- HF_TOKEN — (optional) Hugging Face token to speed up model downloads and avoid anonymous rate limits

These values can be set in `v2/.env` or exported in the environment before starting the server.

## Run commands (recommendations)

Development (reload on change):
```
cd v2
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Production / single-process (recommended for demos):
```
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

If you need to pre-warm the embedding model (avoids first-request delay):
```
python search.py --build --source scrapper/products.jsonl
```

## Curl examples (three-step test: price → feedback → price)

Step 1 — price (before feedback)

```bash
curl -s -X POST http://localhost:8000/price \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Task test — before feedback",
    "metadata": { "region": "ile-de-france" },
    "tasks": [
      {
        "label": "Mortier colle flexible C2",
        "category": "Plumbing",
        "phase": "Install",
        "duration": "3h",
        "quantity": 1
      }
    ],
    "materials": [],
    "contractor_margin": 0.15
  }'
```

Step 2 — submit feedback

```bash
curl -s -X POST http://localhost:8000/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "proposal_id": "test-task-1",
    "item_type": "task",
    "item_label": "Mortier colle flexible C2",
    "feedback_type": "too_low",
    "actual_price": 18.5,
    "comment": "Observed retail price"
  }'
```

Step 3 — re-price (after feedback)

Repeat the Step 1 curl and compare `feedback_adjustment`, `adjusted_cost`, and `with_margin`.

## Feedback math (short)

The adjustment is computed as follows:
- For each stored feedback row with a non-null `actual_price` that fuzzy-matches the item label, compute delta = actual_price − base_price.
- Compute age in days since the feedback row was created.
- Weight each delta by recency: weight = exp(−days_old / 30).
- The feedback_adjustment is the weighted average: sum(delta × weight) / sum(weight). If no matches exist the adjustment is 0.0.

This adjustment is applied additively to the computed `base_cost` (i.e., adjusted = base + adjustment). The mapping is a practical heuristic — not a calibrated probability — and can be replaced with multiplicative or clamped schemes if desired.

## Quick jump (important files)

- `v2/routes.py` — API routes (POST /price, POST /feedback, GET /search, GET /health)  
- `v2/task_pricer.py` — deterministic labor pricing logic and formula  
- `v2/feedback.py` — SQLite feedback store and adjustment computation  
- `v2/search.py` — embedding generation and ChromaDB integration for semantic search  
- `v2/config.py` — configuration and .env handling

## How I tested

Primary manual test flow used in development:
- Build index: `python search.py --build --source scrapper/products.jsonl`
- Start server: `python -m uvicorn main:app --port 8000`
- Run the three-step curl sequence (price → feedback → price) shown above and verify numeric changes.
- Inspect feedback DB: `python feedback.py --list`
- Use CLI helpers: `python feedback.py --adjust --label "Mortier colle flexible C2" --base 15.00`

Automated tests:
- Run: `python -m pytest tests/ -v`
Covered test cases (high level):
1. Health endpoint returns OK + collection size.  
2. Semantic search returns results, respects category filters, rejects empty queries.  
3. Task pricing: base calculation, regional and phase multipliers applied correctly.  
4. Material pricing: semantic match, unit price × quantity, margin application.  
5. Feedback flow: submitting feedback then re-pricing updates adjusted_cost.  
6. Edge cases: empty proposal, unknown material, zero margin handling.

## Getting started (Windows PowerShell)

Quick copy-paste commands for Windows PowerShell:

```powershell
cd v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# (optional, only if you will run the scraper)
python -m playwright install chromium
# Build index (optional, pre-warm model and create Chroma DB)
python search.py --build --source scrapper/products.jsonl
# Start the API (no reload; recommended for demos)
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs` to use the interactive Swagger UI.

## Troubleshooting

- HF_TOKEN / .env pitfalls
  - The project reads `v2/.env` via `v2/config.py`. If you add unknown keys into `.env` pydantic may raise validation errors. Recommended keys are listed in the "Environment variables" section. If you see validation errors referencing extra env keys, either remove those lines from `.env` or add corresponding optional fields in `v2/config.py`.
  - To speed up model downloads and avoid anonymous HF rate limits, set `HF_TOKEN` in your environment before starting the server:
    ```powershell
    $env:HF_TOKEN = "your_hf_token_here"
    python -m uvicorn main:app --host 0.0.0.0 --port 8000
    ```

- Model warm-up time
  - On first run the SentenceTransformer model will be downloaded and materialized — this can take from under a minute to several minutes depending on network and machine. To avoid waiting at API startup, pre-warm by building the index:
    ```
    python search.py --build --source scrapper/products.jsonl
    ```
  - The server startup logs will show model loading progress.

- Playwright / scraper cookies
  - The scraper uses a persistent Chromium profile (`v2/bricodepot_profile`). For reliable headless scraping, run one headful run to accept the cookie banner manually:
    ```
    python scrapper/fetch_products.py --url "https://www.bricodepot.fr/..." --out scrapper/products.jsonl --headful
    ```
  - Accept any cookie dialogs in the opened browser; the consent is then stored in the profile for subsequent headless runs.

- Low or no semantic search results
  - Ensure you ran the index build step and that `CHROMA_PATH` points at the saved index (see `v2/config.py` / `.env`).

- Other
  - If you hit memory or download limits when loading models, consider setting `HF_TOKEN` and/or running the embedding build on a machine with a better connection.


## Tests

15 integration tests covering the full pipeline (no running server needed):

```bash
python -m pytest tests/ -v
```

Test scenarios:
- Health endpoint returns OK + collection size.
- Semantic search returns results, respects category filters, rejects empty queries.
- Water heater pricing: tasks priced, materials matched, regional modifier applied,
  totals consistent, margin correct.
- Feedback flow: submit feedback → re-price → adjusted_cost increases.
- Edge cases: empty proposal, unknown material, zero margin.

## Tech choices

- **Python + FastAPI**: Pydantic validation, automatic OpenAPI docs, async support.
- **Playwright**: bricodepot.fr is JS-rendered; Playwright handles dynamic pages.
- **ChromaDB**: zero-ops local persistence, Python-native API, no external DB.
- **paraphrase-multilingual-MiniLM-L12-v2**: free, no API key, handles French +
  English natively for semantic search.
- **stdlib sqlite3**: zero extra dependency for the single feedback table.
- **JSONL + CLI-first**: simple files, easy debugging, CI-friendly.

## Known limitations

- Regional modifiers are a seed dict (only Île-de-France has a custom value);
  adding more regions requires manual mapping.
- Labor rates are benchmark estimates (midpoint of public ranges), not live
  market data.  The feedback loop is the intended mechanism to converge on
  accurate prices over time.
- The scraper will break if bricodepot.fr changes its DOM structure.  It is
  designed to be re-run periodically, not as a real-time feed.
- No authentication or rate-limiting on the API.

## Source citations (labor rate ranges)

- Habitatpresto — artisan hourly benchmarks:
  https://www.habitatpresto.com/mag/renovation/prix-horaire-artisan
- Travaux.com — electrician pricing guide:
  https://www.travaux.com/electricite/guide-des-prix/prix-dun-electricien
- Ootravaux — tiler hourly benchmarks:
  https://www.ootravaux.fr/construction-renovation/finitions/revetements-sols/carrelage/quels-tarifs-carreleurs.html
- Painting hourly ranges:
  https://www.travauxdepeinture.com/prix/prix-peintre-m2-et-horaire/
- Carpentry/menuiserie hourly ranges:
  https://www.prix-travaux-m2.com/tarifs-menuisier.php
- CAPEB wage grid (indirect — wages < billed rates):
  https://www.capeb.fr/www/capeb/media//aisne/document/Recap%20salaires%20AISNE%20JUILLET%202025.pdf
- INSEE salary statistics (indirect macro benchmark):
  https://www.insee.fr/fr/statistiques/7457170
