# Team USA Hometown Signals & LA28 Momentum API

FastAPI backend for the Team USA Constellation Hub — Cloud Run deployable, Gemini-powered, parity-first.

## License

This project is licensed under the Apache License 2.0. See `LICENSE`.

## Overview

This backend serves the **Hometown Signals** and **LA28 Momentum** challenges from the Team USA Hackathon Hub frontend (`Hub.tsx`). It exposes exactly the endpoints that `Hub.tsx` calls:

| Method | Path | Frontend hook |
|--------|------|--------------|
| `GET` | `/api/hometown/hubs` | `useHometownHubs()` |
| `GET` | `/api/hometown/hubs/{hub_id}` | `useHubDetail(hubId)` |
| `POST` | `/api/hometown/brief` | `useGenerateBrief()` |
| `GET` | `/api/hometown/hubs/{hub_id}/gemini-brief` | Analyst Brief pane (GET) |
| `POST` | `/api/hometown/hubs/{hub_id}/gemini-brief` | Analyst Brief pane (POST + interests) |
| `GET` | `/api/hometown/hubs/{hub_id}/news-pulse` | Live Gemini-grounded news pulse pane |
| `GET` | `/api/la28/momentum` | — |
| `GET` | `/api/la28/momentum/{hub_id}` | — |
| `GET` | `/health` | Cloud Run probe |

Fallback aliases without the `/api` prefix are also registered for resilience.

---

## Architecture

```
hometown_backend_cloudrun/
├── app/
│   ├── main.py                  # FastAPI app, CORS, all routes
│   ├── models.py                # Pydantic models (mirrors Hub.tsx types exactly)
│   ├── services/
│   │   ├── data_store.py        # CSV loader, ApiHubSummary/Detail builders
│   │   ├── gemini_service.py    # Vertex AI + google-generativeai + local fallback
│   │   └── momentum_service.py  # LA28 momentum scores (12 hubs, 6 sports each)
│   └── data/
│       └── hometown_hubs.csv    # 12-hub seed dataset (USOPC 2024 official data)
├── tests/
│   └── smoke_test.py            # Fast smoke tests (no external deps needed)
├── Dockerfile                   # Multi-stage, non-root, Cloud Run ready
├── requirements.txt
├── .env.example
├── deploy.sh                    # Automated Cloud Run deploy script
└── README.md
```

### Design decisions

- **No database** — 12 hubs fit comfortably in memory. The CSV is loaded at startup and cached. A future Cloud SQL or Firestore backend can slot in without changing the API surface.
- **Parity-first** — Every response treats Olympic and Paralympic data with equal weight. The Gemini prompt explicitly mandates it. The `ApiParitySnapshot` model surfaces it structurally.
- **No private athlete data** — All responses use aggregate hometown roster counts from official USOPC spreadsheets. The data store never exposes individual athlete names or personal information.
- **Conditional language** — All AI-generated copy uses words like "could", "may", "might". The momentum scores are explicitly labelled as descriptive indicators, not predictions.

---

## Gemini Integration

The service tries three paths in order:

1. **Vertex AI** (`google-cloud-aiplatform`) — Preferred on Cloud Run. Uses Application Default Credentials automatically. Set `GCP_PROJECT`, `VERTEX_LOCATION`, and `GEMINI_MODEL`.
2. **google-generativeai SDK** — Fallback for local dev or non-GCP environments. Set `GEMINI_API_KEY`.
3. **Local template** — Pre-built conditional phrasing. No external calls needed. Safe for demos with no credentials.

Default model:

```bash
GEMINI_MODEL=gemini-3-flash-preview
VERTEX_LOCATION=global
```

Gemini 3 Flash Preview uses Vertex AI's global endpoint, so keep `VERTEX_LOCATION=global` unless you intentionally switch to a regional Gemini model.

The brief generation prompt instructs the model to:
- Use conditional/descriptive language throughout
- Give Olympic and Paralympic storylines equal prominence
- Exclude any individual athlete names or private data
- End with a compliance disclaimer

---

## Local Development

### Prerequisites

- Python 3.11+
- (Optional) Gemini API key or GCP project with Vertex AI enabled

### Setup

```bash
cd hometown_backend_cloudrun

# Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env to add your GEMINI_API_KEY or GCP_PROJECT

# Run the dev server
uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`. Visit `/docs` for the interactive Swagger UI.

### Configure your frontend

In your Lovable / Vite project, set:

```
VITE_API_BASE_URL=http://localhost:8000
```

---

## Running Tests

```bash
pytest tests/smoke_test.py -v
```

The smoke tests use FastAPI's `TestClient` (no external calls needed):

```
tests/smoke_test.py::test_health PASSED
tests/smoke_test.py::test_list_hubs PASSED
tests/smoke_test.py::test_hub_detail_sd PASSED
tests/smoke_test.py::test_hub_detail_cos PASSED
tests/smoke_test.py::test_hub_not_found PASSED
tests/smoke_test.py::test_brief_local_fallback PASSED
tests/smoke_test.py::test_momentum_list PASSED
tests/smoke_test.py::test_momentum_la PASSED
tests/smoke_test.py::test_alias_routes PASSED
tests/smoke_test.py::test_parity_snapshot_cos PASSED
tests/test_analyst_brief.py::test_get_analyst_brief_basic PASSED
tests/test_analyst_brief.py::test_brief_uses_safe_conditional_wording[...] PASSED
tests/test_analyst_brief.py::test_brief_treats_olympic_and_paralympic_with_equal_prominence PASSED
tests/test_analyst_brief.py::test_safety_scan_rejects_outcome_phrases PASSED
```

---

## Docker

### Build and run locally

```bash
docker build -t hometown-api .
docker run -p 8080:8080 \
  -e CORS_ALLOW_ALL_ORIGINS=true \
  -e LOG_LEVEL=INFO \
  hometown-api
```

### Cloud Run (via deploy.sh)

```bash
export PROJECT_ID=your-gcp-project-id
chmod +x deploy.sh
./deploy.sh
```

Set `DRY_RUN=true` to print commands without executing:

```bash
DRY_RUN=true ./deploy.sh
```

---

## Endpoints Reference

### `GET /health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "gemini_configured": false
}
```

### `GET /api/hometown/hubs`

Returns `ApiHubSummary[]`. 12 hubs, each with `id`, `name`, `region`, `x`, `y`, `short_insight`, `tags`.

### `GET /api/hometown/hubs/{hub_id}`

Returns `ApiHubDetail` with real lat/lng coordinates, map pins (Olympic/Paralympic signal pins), parity snapshot, and source attribution.

Example: `GET /api/hometown/hubs/cos`

### `POST /api/hometown/brief`

Request:
```json
{
  "hub_id": "cos",
  "interests": ["wheelchair_basketball", "altitude_training"]
}
```

Response: `ApiBriefResponse` with AI-generated or template brief, themes list, and compliance disclaimer.

### `GET /api/hometown/hubs/{hub_id}/gemini-brief`

Structured **Analyst Brief** for a hub — designed for the frontend Analyst Brief pane that opens when a hometown hub is selected. The response combines the hub detail, parity snapshot, map pins, tags, and LA28 momentum signals into a sectioned brief.

`POST` is supported with the same path and an optional body for interest weighting:

```json
{ "interests": ["wheelchair_basketball", "altitude_training"] }
```

Response (`AnalystBriefResponse`):

```json
{
  "hub_id": "cos",
  "hub_name": "Colorado Springs",
  "generated_with_gemini": false,
  "model": null,
  "source": "local-fallback",
  "sections": [
    { "title": "Hometown Snapshot", "body": "..." },
    { "title": "Olympic Signal", "body": "..." },
    { "title": "Paralympic Signal", "body": "..." },
    { "title": "LA28 Momentum", "body": "..." },
    { "title": "What Fans Could Watch For", "body": "..." }
  ],
  "key_takeaway": "Colorado Springs' hometown signals could help fans...",
  "disclaimer": "All counts are aggregate hometown roster entries...",
  "generated_at": "2026-05-03T17:21:09+00:00"
}
```

Behaviour:

- Tries Vertex AI first, then `google-generativeai`, then a deterministic local template. When live generation is used and survives the safety scan, `generated_with_gemini` is `true` and `model` reports the model id; otherwise `generated_with_gemini` is `false` and the response is the safe local fallback.
- All copy is conditional/descriptive: `could`, `may`, `might`, `suggests`, `signals`. Service-side scan rejects model output that contains medal-prediction language (`will win`, `guaranteed`, `lock for gold`, etc.) and falls back to the local template.
- Olympic and Paralympic signals appear as side-by-side sections with equal prominence.
- No individual athlete names or private data — only aggregate roster counts and public sport-culture context.

### `GET /api/hometown/hubs/{hub_id}/news-pulse`

Live, **Gemini-grounded** news pulse for a hometown hub. Each request asks Gemini (Vertex AI grounding or `google-generativeai` with the `google_search` tool) to fetch 2–3 fresh public news items connected to the hub. Nothing is stored in a database or seed file.

Topic scope (any of the following, picked live by the model):

- Hometown Olympians or Paralympians from the hub or its region.
- Team USA / U.S. Olympic / U.S. Paralympic team news that ties to the hub.
- LA28-related news connected to the hub or its sports.

Response (`NewsPulseResponse`):

```json
{
  "hub_id": "sd",
  "hub_name": "San Diego",
  "generated_with_gemini": true,
  "source": "gemini-search",
  "model": "gemini-3-flash-preview",
  "query": "Recent Team USA, Olympic, Paralympic, and LA28 news connected to San Diego, ...",
  "cards": [
    {
      "title": "...",
      "summary": "...",
      "category": "olympic | paralympic | team-usa | la28",
      "source_label": "Team USA",
      "source_url": "https://www.teamusa.com/news/...",
      "published_date": "2025-11-12"
    }
  ],
  "brief": "1-2 sentence overview of the live news pulse.",
  "disclaimer": "News cards are fetched live via Gemini search/grounding...",
  "generated_at": "2026-05-04T15:21:09+00:00"
}
```

Behaviour:

- Tries **Vertex AI grounding** first (`source: "vertex-grounded"`), then **`google-generativeai` with the `google_search` tool** (`source: "gemini-search"`).
- Each backend is invoked across **up to four query angles** before giving up:
  1. General hub Team USA / Olympic / Paralympic / LA28 prompt.
  2. `"{hub_name} Team USA Olympian news"`.
  3. `"{hub_name} Paralympic athlete Team USA news"`.
  4. `"{hub_name} LA28 Olympic news"`.

  The first angle that produces validated cards wins.
- For each backend response we accept either (a) a strict JSON cards payload, **or** (b) source-backed cards reconstructed from the response's grounding metadata / citation chunks (`candidate.grounding_metadata.grounding_chunks[*].web.uri` or `candidate.citation_metadata.citations[*].uri`). This covers the case where Gemini returns natural-language prose with citations rather than a JSON document.
- If every angle fails to produce parseable JSON **and** no grounding URLs survive, the response returns `generated_with_gemini: false`, `source: "unavailable"`, an empty `cards` list, and a `brief` explaining live news could not be fetched. **No invented or fallback news is ever returned.**
- Every card is required to carry a public http(s) `source_url`; cards without a usable URL are dropped before the response is returned. URLs are taken verbatim from the model's JSON output or the response's grounding metadata — never synthesised.
- Cards whose model-generated copy contains ranking / endorsement / outcome language (`best`, `top`, `major`, `will win`, `guaranteed`, etc.) are dropped by the safety scan. Titles harvested from publisher metadata are softly scrubbed (`top` → `notable` etc.); any title that still trips the scan is dropped.
- Olympic and Paralympic stories are surfaced with equal prominence; the brief uses conditional / descriptive language only.
- The endpoint **does not persist** any news to a database or file — every response is freshly generated.

#### Cloud Run env requirements for live News Pulse grounding

For the endpoint to return live cards (rather than `source: "unavailable"`), at least one of the following Gemini paths must be reachable at runtime:

**Path A — Vertex AI grounding (preferred on Cloud Run)**

| Env var | Value | Notes |
|---|---|---|
| `GCP_PROJECT` *(or `GOOGLE_CLOUD_PROJECT`)* | your GCP project id | Required. Triggers the Vertex AI path. |
| `VERTEX_LOCATION` | `global` | Default. Gemini 3 Flash Preview uses Vertex's global endpoint. |
| `GEMINI_MODEL` | e.g. `gemini-3-flash-preview` | Optional. Defaults to the value in `app/services/gemini_service.py`. |

In addition, the Cloud Run service account needs:

- The **Vertex AI User** role (`roles/aiplatform.user`) on the project.
- The **Vertex AI API** enabled on the project (`gcloud services enable aiplatform.googleapis.com`).
- Access to the **Google Search grounding tool** for Gemini. On Cloud Run this works out of the box for projects with the Vertex AI API enabled — the SDK call uses `Tool.from_google_search_retrieval(grounding.GoogleSearchRetrieval())` and falls back to `Tool.from_google_search()` for newer SDK builds.
- Application Default Credentials (ADC) — Cloud Run injects these automatically when the service is deployed with `--service-account=<sa@project.iam>`.

**Path B — `google-generativeai` SDK fallback**

| Env var | Value | Notes |
|---|---|---|
| `GEMINI_API_KEY` | your AI Studio API key | Enables the `google_search` tool in the `google-generativeai` SDK. |
| `GEMINI_MODEL` | e.g. `gemini-3-flash-preview` | Optional. |

Useful smoke checks after a redeploy:

- `GET /health` returns `{"gemini_configured": true}`.
- Cloud Run logs show `NEWS_PULSE: N cards via vertex-grounded on angle '...'` (or `gemini-search`).
- A failure typically surfaces as `NEWS_PULSE: Vertex grounded generation failed: ...` — the most common causes are (1) the service account missing `aiplatform.user`, (2) the Vertex AI API not being enabled, or (3) the grounding tool not being available for the chosen model in the chosen region.

### `GET /api/la28/momentum`

Returns `LA28MomentumSummary[]` for all 12 hubs — top sport, top score, Olympic/Paralympic sport counts.

### `GET /api/la28/momentum/{hub_id}`

Returns `LA28MomentumResponse` with all sports for a hub, momentum scores, signal breakdowns, and reason text.

Example: `GET /api/la28/momentum/la`

---

## Data Sources

All data is derived from publicly available sources:

- **USOPC 2024 Olympic Roster**: [official spreadsheet](https://assets.contentstack.io/v3/assets/blt9e58afd92a18a0fc/bltfbab8d857574a719/672e564b824c1a33908da119/2024_U.S._Olympic_Team_610_Final.xlsx)
- **USOPC 2024 Paralympic Roster**: [official spreadsheet](https://assets.contentstack.io/v3/assets/blt9e58afd92a18a0fc/bltb157d69d46ed67bc/66c3864688b74e0c81b14946/2024_Paralympic_Roster_State_Sport.xlsx)
- **Coordinates**: U.S. Census Bureau 2024 Gazetteer Files

---

## Data Integrity Notes

- `olympian_count` and `paralympian_count` are aggregate **roster hometown entry counts** from official USOPC spreadsheets — not medal counts.
- A blank `paralympian_count` means no official Paralympic roster hometown entry appeared for that city in the parsed USOPC spreadsheet — not a performance judgment.
- All copy uses conditional wording such as "could help fans discover" or "may suggest story patterns."
- Geography does not guarantee athlete outcomes and is never implied to do so.


---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT` | — | GCP project for Vertex AI |
| `VERTEX_LOCATION` | `us-central1` | Vertex AI region |
| `GEMINI_API_KEY` | — | google-generativeai SDK key (fallback) |
| `CORS_ALLOW_ALL_ORIGINS` | `false` | Set `true` for hackathon demo |
| `CORS_ALLOWED_ORIGINS` | (see main.py) | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `PORT` | `8080` | Server port (injected by Cloud Run) |
