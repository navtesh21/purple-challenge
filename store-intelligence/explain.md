# explain.md — the whole system, in plain language

> **Final-dataset update (v2):** the event schema is now the dataset's real three-family wire
> format (entry/zone/queue) plus an added `visitor_id`, ingested via a boundary normalizer
> (`app/schema_compat.py`); two stores (ST1008 + ST2002); conversion also uses the billing
> `queue_completed`/served signal. See **docs/SCHEMA.md**. Where sections below mention a single
> unified event schema with `dwell_ms`/`ZONE_DWELL`, that's the earlier design — the canonical
> internal record is the same, but the wire contract is now the three families.

This file is written so **you can explain every line and every decision** in the follow-up
interview. It walks the codebase module by module: what it does, *why* it's built that way,
what I considered and rejected, and what breaks at scale. Read it top to bottom once and you
will be able to defend the entire design.

> The challenge's follow-up questions are designed to be un-answerable generically — they probe
> *your own* code. So this doc leans into the trade-offs and failure modes, not just the happy path.

---

## 0. The mental model (read this first)

The business problem: Apex Retail has rich **online** analytics but its **offline** stores are a
data blind spot. We turn CCTV video into the same kind of funnel/conversion analytics an
e-commerce team already has.

The single number everything serves is the **North Star: offline conversion rate** =
`visitors who bought ÷ unique visitors`. Every stage either makes that number *more accurate*
(detection) or *more useful* (API/dashboard).

The pipeline is four hops:

```
CCTV clips ──► Detection ──► Events (one schema) ──► Intelligence API ──► Live dashboard
(input)       (pipeline/)    (the contract)          (app/)               (dashboard/)
```

The **event schema is the contract** between the two halves. Get that right and the detection
side and the API side can be reasoned about independently. That's why `app/models.py` is
imported by *both* the pipeline and the API — producer and consumer can never drift.

### One decision that colours everything: what is a "visitor"?

`visitor_id` is a **stable Re-ID token for one physical person**, not "one entry". The whole
point of Re-ID is to solve **re-entry inflation** (a known vendor problem the brief calls out).
So:

- The same person who leaves and comes back keeps the **same** `visitor_id` and produces a
  `REENTRY` event — *not* a second `ENTRY`.
- A "session" for funnel/conversion is therefore **keyed by `visitor_id`**.
- "Unique visitors" = count of distinct `visitor_id` (staff excluded).

This single choice is what makes "re-entries must not double-count" fall out automatically
everywhere downstream, instead of being special-cased in each endpoint.

---

## 0b. The REAL data (and why the system runs on it, not synthetic)

The challenge ships a single real store: **Purplle, Brigade Road, Bangalore — `ST1008`**,
five CCTV clips (`CAM 1–5.mp4`, ~2.5 min each, 1080p) and the store's **real POS export**
(2026-04-10, 24 orders). The **Evaluation Framework** has a hard rule: *"outputs do not vary
with input" / "lack of real computation" → score capped at 50.* So the system must run **real
detection on the real footage** — which it does. The synthetic generator (`data/generate.py`)
is demoted to a dev/test fallback that writes to `data/synthetic/` and can never clobber the
real `data/` files.

The camera→role mapping below was inferred from sample frames and then **confirmed against the
official floor plan** (embedded images in `Brigade Road - Store layout.xlsx`: entrance left,
skincare wall top, makeup→hair wall bottom, cash counter right, back-room Access door). So this
mapping is grounded in the provided plan, not just a guess.

**How I read the store from the footage** (`tools/probe_videos.py` dumps sample frames):
- CAM 3 → **entrance** (glass-door threshold) → `CAM_ENTRY_01`
- CAM 1 → **skincare floor** (The Face Shop, Minimalist…) → `CAM_SKIN_01`
- CAM 2 → **makeup/hair floor** → `CAM_MAKEUP_01`, pixel-polygon sub-zoned into MAKEUP (makeup wall, right) and HAIRCARE (Alps/accessories, left)
- CAM 5 → **billing counter** (POS terminals) → `CAM_BILLING_01`
- CAM 4 → **back/stock room** → `CAM_STOCK_01` (staff only → everything here is `is_staff`)

**Time alignment:** the clips carry a **burned-in timestamp** (~20:10 IST, 2026-04-10).
`store_layout.json` records each camera's UTC `start` + `fps`, so the pipeline stamps real
wall-clock event times that line up with the POS log. The POS is line-item level; `prepare_pos.py`
collapses 101 rows → 24 orders, IST→UTC (fixed +5:30; India has no DST and this box has no tz
database).

**The honest conversion result:** the clip window (≈14:40–14:43 UTC) contains **no POS
transaction within the 5-min correlation window** (nearest sale ~13 min later), so the
correctly-computed conversion for the clip is ~0. I report it honestly and explain it
(CHOICES.md / DESIGN.md), and demonstrate the conversion machinery with non-zero values via the
simulator and a test that aligns a billing visitor to a real POS timestamp. The follow-up
question this anticipates: *"your conversion is 0 — is it broken?"* → No; the 2.5-min clip
simply captured no sale, and I can show exactly why.

**Self-sufficiency:** `docker compose up` auto-seeds the committed `data/events.jsonl`
(footage-derived) and `data/pos_transactions.csv` on first boot (via the same validated ingest
path), so `/metrics` returns real numbers with zero manual steps — satisfying the acceptance
gate cleanly.

## 1. Repository layout

```
store-intelligence/
├── app/            # the Intelligence API (Part B + C)
│   ├── config.py        # every tunable threshold, env-overridable
│   ├── models.py        # the canonical event schema (Pydantic) — the CONTRACT
│   ├── db.py            # SQLAlchemy ORM, engine, tz handling, graceful-degradation hook
│   ├── layout.py        # reads store_layout.json: zones, billing zone, open hours
│   ├── ingestion.py     # POST /events/ingest logic: validate, dedup, idempotent
│   ├── analytics.py     # SHARED core: events -> visitor sessions + POS conversion
│   ├── metrics.py       # GET /metrics
│   ├── funnel.py        # GET /funnel
│   ├── heatmap.py       # GET /heatmap
│   ├── anomalies.py     # GET /anomalies
│   ├── health.py        # GET /health
│   ├── logging_mw.py    # structured JSON request logging
│   └── main.py          # FastAPI wiring, exception handlers, startup, dashboard mount
├── pipeline/       # the Detection pipeline (Part A)
│   ├── detect.py        # CCTV clips -> events (YOLO + ByteTrack orchestration)
│   ├── tracker.py       # Re-ID gallery, line-crossing, staff, zone mapping, dwell
│   ├── emit.py          # build + transport schema-valid events (file or HTTP)
│   └── run.sh           # one command: all clips -> events (-> optionally the API)
├── tools/
│   ├── simulate.py      # replay a log OR stream live events (Part E bridge)
│   └── smoke.py         # fast in-process sanity check during dev
├── data/
│   ├── generate.py      # synthetic dataset + event generator (footage-free demo)
│   ├── store_layout.json, pos_transactions.csv, sample_events.jsonl, events.jsonl
├── dashboard/index.html # the live web UI (Part E)
├── tests/               # pytest suite, prompt blocks atop each file
├── docs/DESIGN.md, docs/CHOICES.md
├── docker-compose.yml, Dockerfile, README.md
```

**Why split `app/` and `pipeline/` so hard?** The API must be a small, fast container that an
ops team runs. The CV stack (torch, ultralytics, opencv) is ~2 GB and irrelevant to serving
analytics. Two requirements files, two concerns. The only thing they share is `app/models.py`
(the schema), which the pipeline imports via a tiny `sys.path` bootstrap.

---

## 2. The event schema — `app/models.py` (the contract)

Pydantic v2 models mirroring the required output schema exactly. Highlights and the reasoning:

- **`EventType` is an `Enum`.** Anything outside the eight allowed types is rejected at the
  door. This is half of "schema compliance" scoring.
- **`confidence` is `ge=0.0, le=1.0` and we never drop low-confidence events.** The brief is
  explicit: "do not suppress low-conf events." Suppressing them would silently bias counts.
  Instead we *store* the confidence and let consumers decide. Confidence calibration is a
  scored criterion, so honesty here matters.
- **`timestamp` validator forces UTC.** Naive datetimes are assumed UTC (the pipeline emits
  UTC); offset-aware ones are converted. Every timestamp in the system is one tz. This kills an
  entire class of "can't compare offset-naive and offset-aware datetime" bugs.
- **`metadata` allows extra keys (`extra="allow"`).** The pipeline may attach diagnostics
  (e.g. the bounding box, the matched gallery id). We'd rather keep them than reject an
  otherwise-valid event. Required sub-fields (`queue_depth`, `sku_zone`, `session_seq`) are
  still typed.
- **`use_enum_values=True`** so the model serialises `event_type` straight to the string the DB
  stores — no enum/str juggling later.

The `Ingest*` models define the request/response shape: `IngestResponse` reports
`received / accepted / duplicates / rejected` plus a per-item `errors` list. That 4-way split is
what makes **partial success** observable to the caller.

---

## 3. Configuration — `app/config.py`

A single `Settings` object, every value `os.getenv(...)`-overridable, `@lru_cache`d. The point:
**no magic numbers buried in business logic.** The conversion window (300s), the
queue-spike thresholds, the stale-feed lag (600s), the "min sessions for confidence" (20) — all
named here. When an interviewer asks "where's the 5-minute window defined?", it's one line:
`conversion_window_s`. The same image runs on Postgres (compose sets `DATABASE_URL`) or SQLite
(default) with zero code change.

---

## 4. Persistence — `app/db.py`

SQLAlchemy 2.0 ORM. Decisions:

- **Two tables only: `events` and `pos_transactions`.** Everything else (sessions, funnels,
  metrics) is *computed on read* from `events`. Why not pre-aggregate? Because the brief demands
  **real-time, not cached-from-yesterday** numbers, and at this data scale (one store-day is
  ~10k events) on-read computation is milliseconds. Pre-aggregation would add a staleness bug
  surface for no benefit *yet* (see §15 for when that changes).
- **Portable column types only.** No Postgres-specific types, so the identical models run on
  SQLite for tests. `JSON` type works on both.
- **Timestamps stored tz-naive UTC.** Postgres and SQLite disagree on tz-aware storage. I
  normalise to naive-UTC on write (`to_naive_utc`) and re-attach UTC on read (`as_utc`). Every
  comparison in the codebase is then in one tz. This is a deliberate, documented convention, not
  an accident.
- **`event_id` is the primary key.** That's what makes ingest idempotent for free: a repeated
  event collides on PK and we skip it.
- **Indexes** on `store_id`, `visitor_id`, `event_type`, `is_staff`, `ts` — the columns every
  analytics query filters on.
- **`DBUnavailable`** custom exception + `pool_pre_ping=True`. If Postgres drops, `get_session`
  catches `OperationalError` and raises `DBUnavailable`, which `main.py` maps to a structured
  **HTTP 503** (Part C: graceful degradation, no stack traces). `ping()` powers `/health`.
- **`reset_engine()`** exists so tests can swap `DATABASE_URL` between cases.

---

## 5. Store layout — `app/layout.py`

Reads `store_layout.json` (cached). Answers the structural questions the analytics need:

- `billing_zone(store)` — which zone is the checkout. First by explicit `type: "billing"`, then
  by name convention (BILLING/CHECKOUT/CASH/…). This is load-bearing: conversion correlation
  keys off "was the visitor in the billing zone".
- `product_zones(store)` — the zones that count as a "Zone Visit" in the funnel and appear in
  the heatmap (excludes threshold + billing).
- `open_hours`, `sku_zone(zone)` — used by anomalies (dead-zone gating) and enrichment.

If the file is missing it degrades to an empty layout rather than crashing — the API still
serves, billing zone is inferred by name. Defensive on purpose.

---

## 6. Ingestion — `app/ingestion.py`  (POST /events/ingest)

The order of operations *is* the design:

1. **Batch-size guard.** Over `ingest_max_batch` (500) → raise `ValueError` → 400. We reject an
   oversize batch loudly instead of silently truncating (silent truncation reads as success but
   loses data).
2. **Per-item validation.** Each raw event is validated independently. A bad one is recorded in
   `errors` with its index and a short reason; the good ones still go through. **This is partial
   success** — one malformed event never sinks a 500-event batch.
3. **In-batch dedup.** `dict.setdefault(event_id, ...)` keeps the first occurrence of any id
   repeated within the same payload.
4. **Idempotency across batches.** We query which `event_id`s already exist (chunked `IN()` so a
   big batch stays within driver limits) and insert only the new ones. Re-sending the exact same
   payload returns `accepted=0, duplicates=N` and changes nothing. **Tested explicitly** — Part C
   requires it.
5. Commit, return the 4-way count.

> **Follow-up Q I can answer:** "What if two requests with the same event_id race?" — On Postgres
> the second `INSERT` hits the PK constraint; the simple fix (and what I'd add for true
> concurrency) is `INSERT ... ON CONFLICT DO NOTHING`. The current check-then-insert is correct
> for the single-writer replay path and the test suite; the upsert is the one-line hardening for
> many concurrent producers (40 live stores — see §15).

---

## 7. The analytics core — `app/analytics.py`  (the most important file)

Every read endpoint is built on **one** function: `build_window(store, start, end)`, which
collapses raw events into `VisitorSession` objects keyed by `visitor_id`. Doing this once, in
one place, means metrics / funnel / heatmap all agree by construction.

For each session we accumulate: whether they entered/re-entered, which product zones they
visited, max dwell per zone, whether they reached billing, their billing-presence timestamps,
whether they abandoned, the max queue depth they saw, and their min detection confidence.

**Staff exclusion** happens here in the SQL (`is_staff == False`), so no customer-facing number
ever includes staff. Single chokepoint.

**Conversion (POS correlation)** — the North Star:

- Pull POS transactions for the store in the window.
- A visitor is **converted** if any of their billing-presence timestamps has a transaction
  within `[presence, presence + 300s]`. Implemented with a binary search (`bisect`) over sorted
  transaction times — O(log n) per presence instead of scanning all txns.
- There is no `customer_id` in POS data (by design), so correlation is **time-window + store**,
  exactly as the brief specifies. This is inherently fuzzy: in a crowd, two visitors at billing
  near the same transaction both look "converted". I accept that and document it; the brief
  explicitly says correlation is by time window, and the metric is about *rates/trends*, not
  per-person billing.

**`default_window`** — what does "today" mean? I anchor the day to the **store's freshest event**,
not wall-clock midnight. Why: it works identically for a live feed (freshest event = now → today)
and a historical replay (freshest event = that day), and it can never return "yesterday's cache".
An empty store falls back to the wall-clock UTC day. This is the single most subtle decision in
the analytics and I can defend it: it makes the same code correct in demo and in production.

`StoreWindow` exposes `unique_visitors`, `converted_visitors`, `conversion_rate` — and
`conversion_rate` is **zero-traffic safe** (no visitors → 0.0, never a divide-by-zero or null).

---

## 8. The five read endpoints

### `metrics.py` — GET /stores/{id}/metrics
Headline numbers for the window: unique visitors, conversion rate, purchases + basket value,
avg dwell **per zone** (ms → seconds, averaged over visitors who dwelt there), current queue
depth (the most recent observed `queue_depth`), billing visitors, abandonment rate, and an
explicit `is_zero_traffic` flag. Everything is well-formed zeros on an empty/zero-purchase store
— never null, never 500.

### `funnel.py` — GET /stores/{id}/funnel
Four stages: **Entry → Zone Visit → Billing Queue → Purchase**, counted in **sessions**. Each
stage reports `count`, `drop_off_from_previous` (% lost from the prior stage), and `pct_of_entry`.
Because the unit is `visitor_id`, a re-entering visitor is counted once per stage. The funnel is
where "where are we losing customers?" gets answered.

### `heatmap.py` — GET /stores/{id}/heatmap
Per **product** zone: visit count + avg dwell, each normalised 0–100 against the product-zone
maxima, ready to drop into a grid. Includes every product zone (even zero-traffic ones) for a
stable grid. Emits `data_confidence: LOW` when the window holds fewer than 20 sessions, so the
dashboard can grey-out a noisy heatmap instead of presenting noise as signal.

### `anomalies.py` — GET /stores/{id}/anomalies
Three detectors, each returns `severity` (INFO/WARN/CRITICAL) + a concrete `suggested_action`:
- **BILLING_QUEUE_SPIKE** — most recent queue depth in the last 10 min past WARN(5)/CRITICAL(8).
- **CONVERSION_DROP** — today's conversion vs the trailing N-day (default 7) baseline; relative
  drop past 20% (WARN) / 40% (CRITICAL). Needs ≥2 days of history or it stays quiet (no crying
  wolf on a cold start).
- **DEAD_ZONE** — a product zone with no visits in 30+ min, **gated by open hours** and only
  once the store has had traffic today (otherwise it's just zero-traffic, not an anomaly).
"Now" is anchored to the store's freshest event so detectors behave the same live and on replay.

### `health.py` — GET /health
The on-call view. Service + DB status, and per store the last event timestamp + feed lag,
flagging **STALE_FEED** past 10 min. Lag is measured against **wall-clock now** on purpose — an
honest health check: historical data really is stale, a live feed is fresh. It's defensive: if
the DB is down it returns `status: degraded` with a structured body rather than throwing, because
that's exactly when someone is reading it.

---

## 9. Structured logging — `app/logging_mw.py`
A Starlette middleware emits **one JSON line per request** with `trace_id, store_id, endpoint,
method, status_code, latency_ms, event_count`. `trace_id` is generated per request (or taken from
an inbound `X-Trace-Id`) and echoed back in the response header, so a client call can be tied to a
log line. `store_id` is parsed from the path; `event_count` is filled in by the ingest route via
`request.state`. This is the Part C "structured logs" requirement, done as real ops tooling.

## 10. App wiring — `app/main.py`
FastAPI app + the middleware + the routes. Plus:
- **Lifespan startup**: configure logging, create tables, **seed POS** from the CSV if the table
  is empty (so a fresh container has working conversion numbers with no manual import).
- **Exception handlers**: `DBUnavailable → 503`, `ValueError → 400`, both structured with the
  `trace_id`, no stack traces leaked.
- **Dashboard mount**: serves `dashboard/index.html` at `/` and static assets at `/static`.

---

## 11. The detection pipeline — `pipeline/`  (Part A)

> No footage was provided in the dataset drop, so this code is built to run on real clips the
> moment they're placed in `data/clips/<STORE>/<CAMERA>.mp4`, and the synthetic generator +
> simulator stand in for the demo. The pipeline emits the **identical schema**, so nothing
> downstream changes when real video arrives.

**`detect.py`** orchestrates per camera: YOLO person detection + ByteTrack tracking
(`model.track(..., tracker="bytetrack.yaml", classes=[0])`), then for each tracked box per frame:
crop → appearance signature → `ReIDGallery` → stable `visitor_id`; centroid → `ZoneMapper` →
zone; on the entry camera, `LineCrossing` → ENTRY/EXIT/REENTRY; on floor/billing cameras, zone
transitions → ZONE_ENTER/EXIT, ZONE_DWELL every 30s, BILLING_QUEUE_JOIN. Frame timestamps =
clip-start + frame_index/fps (clip start read from a sidecar JSON or a deterministic default).

**`tracker.py`** is the reasoning layer — deliberately **classical and explainable**, not a heavy
Re-ID net:
- **`appearance_signature`** — HSV hue+saturation histogram, L1-normalised. Value channel dropped
  to blunt the lighting variation the brief warns about. Cheap, illumination-tolerant.
- **`ReIDGallery`** — maps unstable per-camera `track_id`s to stable `visitor_id`s by cosine
  similarity (threshold 0.72) within a 15-min TTL. A match landing on an already-EXITed entry is a
  **re-entry**. A match across cameras is **cross-camera dedup** (the floor/entry overlap). No
  match → a fresh visitor. The signature is rolling-averaged so it tracks lighting drift.
- **`LineCrossing`** — entry vs exit by the **sign of the 2D cross product** of the door line vs
  the centroid; a sign change is a crossing, its direction tells inbound from outbound. This is
  why ENTRY and EXIT are about *direction*, not mere presence — and why a **group of 3** produces
  **3 ENTRY events** (three tracks each cross the line) not 1.
- **`StaffClassifier`** — flags staff by a configured uniform-colour signature, with a `vlm_hook`
  so a VLM (e.g. Claude Vision) can adjudicate ambiguous crops. CHOICES.md records the VLM prompt
  and whether it earned its place.
- **`ZoneMapper`** — point-in-polygon (ray casting) against per-camera zone polygons in the
  layout; falls back to the camera's single covered zone when no polygons are declared.

**`emit.py`** — `make_event()` validates against `app.models.Event` *before* the event leaves the
pipeline (a bad event fails here, loudly, not on ingest). Two sinks: `JsonlSink` (batch file) and
`HttpSink` (batched POST to the API). One schema-valid code path for everything.

### The edge cases (all 7), and where each is handled
| Edge case | Where |
|---|---|
| Group entry (count individuals) | `LineCrossing` per track → N ENTRY events |
| Staff movement | `StaffClassifier.is_staff` → `is_staff=true` → excluded in `analytics.py` SQL |
| Re-entry | `ReIDGallery` match on an EXITed entry → `REENTRY`, same `visitor_id` |
| Partial occlusion | confidence preserved (never suppressed); rolling signature survives short occlusion |
| Billing queue buildup | `BILLING_QUEUE_JOIN` carries `queue_depth`; metrics/anomalies read it |
| Empty store periods | zero-traffic-safe analytics; `is_zero_traffic`; no crash/null |
| Camera angle overlap | `ReIDGallery` cross-camera match → same person not double-counted |

---

## 12. Synthetic data — `data/generate.py`

Because there's no footage, this writes a **self-consistent** dataset so the whole system runs
and is testable, and is reused by the live simulator. It's seeded (`random.Random(7)`) for
reproducibility. It models all 7 edge cases. Crucially it builds a **real arrival/service queue
model**: billing visitors get a join time and a service time; queue depth at join = how many are
still being served — so queue buildup and abandonment **emerge** rather than being faked.

It generates 8 days. Earlier days carry a healthy conversion baseline; **today is deliberately
depressed** (lower conversion, a cold WELLNESS zone, a billing rush) so the anomaly detectors
have real signal to find. POS rows are written only for converting visitors, at a time inside the
conversion window — so conversion correlation actually resolves.

> This is not a "hack" to fake results: the API computes every number from these events the same
> way it would from pipeline output. The generator is a stand-in *producer*, nothing more.

## 13. Replay / live simulator — `tools/simulate.py`  (Part E bridge)
- `--from-file events.jsonl` — backfill a recorded log into the API in batches (the historical
  load). `--rt` paces it by real time for a "watch it fill" effect.
- `--live` — spawns fresh visitors continuously, each event **stamped at wall-clock now** and
  POSTed immediately, so `/health` stays fresh, `/metrics` ticks up, and the dashboard animates.
  It intentionally builds a billing queue (queue depth climbing into WARN/CRITICAL) and keeps one
  zone cold, so the anomaly endpoint shows **live** signal. This is the proof the pipeline and API
  are genuinely connected, not just batch-processed.

## 14. The dashboard — `dashboard/index.html`  (Part E)
A single self-contained file (no build step → trivial to containerise) served by FastAPI. Polls
all five endpoints every 2s and renders: KPI cards (with a flash animation on change), the
conversion funnel as bars, the zone heatmap as an intensity grid, active anomalies with severity
chips + suggested actions, and the feed-health table with a live status dot. Design: a dark
operations console, OKLCH tokens, one indigo accent, accessible contrast, `prefers-reduced-motion`
honoured. It reads as a tool an ops team would actually keep open.

---

## 15. "At 40 live stores, what breaks first?" (the scale question)
Honest answer, in order:
1. **On-read aggregation.** Each `/metrics` call re-scans a store-day of events. Fine at this
   scale; at 40 stores × all-day × dashboards polling every 2s it becomes the bottleneck. Fix:
   a rollup table updated on ingest, or a short TTL cache per (store, window).
2. **Ingest concurrency.** The check-then-insert idempotency has a race under many concurrent
   producers. Fix: `INSERT ... ON CONFLICT DO NOTHING` (Postgres upsert).
3. **POS correlation cost.** Re-pulling and re-sorting txns per request. Fix: cache sorted txn
   times per store-day.
4. **SQLite.** Single-writer; compose already uses Postgres for exactly this reason.
None of these are wrong *now* — they're the documented next steps, which is the point of the
North Star framing: optimise only what makes the number more accurate or more useful.

---

## 16. How to run it (the 5 commands)
```
docker compose up --build -d                 # 1. API + Postgres
python tools/simulate.py --from-file data/events.jsonl   # 2. backfill history
python tools/simulate.py --live --store STORE_BLR_002    # 3. live stream
# open http://localhost:8000                  # 4. dashboard
pytest --cov=app                              # 5. tests + coverage
```
(Full setup and the footage path are in README.md.)

---

*This document is updated as the build evolves. If something in the code contradicts this file,
the code is right and this file is stale — tell me and I'll fix it.*
