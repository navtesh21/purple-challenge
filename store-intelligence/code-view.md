# code-view.md — a file-by-file reading companion

> **Final-dataset update (v2):** one new file to read — **`app/schema_compat.py`**
> (`normalize_event`): the boundary that maps the dataset's three real event shapes
> (entry/zone/queue) + our pipeline output + the old shape into the one canonical record. It's
> called at the top of `app/ingestion.py`'s validate loop. `pipeline/emit.py` now has
> `make_entry`/`make_zone`/`make_queue` (the three wire shapes + `visitor_id`) instead of one
> `make_event`. Two stores live in `data/store_layout.json`. Authoritative schema: **docs/SCHEMA.md**.

Open each source file next to its section here. For every file you get: **what it is**, the
**public symbols** (functions/classes you'll call or see called), an **annotated walk** of the
load-bearing lines, and **gotchas**. Read in the order below; it follows the data, not the
alphabet.

If a concept is unfamiliar (detection, Re-ID, cosine similarity, sessions, conversion window),
read `LEARN.md` first — this doc assumes you know *what* the pieces are and focuses on *how the
code does it*.

**Map of the whole thing (who imports whom):**

```
                         app/models.py  (the Event schema — the CONTRACT)
                          ▲                         ▲
            pipeline/emit.py                   app/ingestion.py
                 ▲                                  ▲
   pipeline/tracker.py     app/db.py ──────────────┤        app/config.py (read by ~everything)
                 ▲            ▲                     │        app/layout.py (reads store_layout.json)
   pipeline/detect.py        └── app/analytics.py ─┤
   (CCTV → events.jsonl)            ▲              │
                                    │      app/{metrics,funnel,heatmap,anomalies,health}.py
                                    └──────────────┘
                                          ▲
                                    app/main.py  (FastAPI: routes + startup + error handlers)
                                          ▲
                                    dashboard/index.html (polls the GET endpoints)
tools/simulate.py  ── posts events ──►  app/main.py  (/events/ingest)
```

Two independent halves joined only by `app/models.py`. The left half (`pipeline/`) makes events;
the right half (`app/`) consumes them. You can read either half without the other.

---

# PART A — THE API (`app/`)

## `app/models.py` — the event schema (read this FIRST; everything depends on it)
**What:** the one definition of an event, using **Pydantic** (a library that validates that a
Python dict matches a declared shape). Imported by *both* halves so they can't drift.

**Public symbols:** `EventType` (enum of the 8 types), `EventMetadata`, `Event`, `IngestRequest`,
`IngestItemError`, `IngestResponse`.

**Annotated walk:**
- `class EventType(str, Enum)` — the 8 allowed verbs. Being a `str` enum means it serialises to a
  plain string in JSON. Anything not in this list is rejected.
- `Event` fields each carry rules: `confidence: float = Field(..., ge=0.0, le=1.0)` means "required,
  between 0 and 1." `event_id` is required and non-empty. `metadata` defaults to an empty
  `EventMetadata` (so it's never missing).
- `model_config = ConfigDict(use_enum_values=True)` — store the enum's *string value*, not the enum
  object, so downstream code compares `event_type == "ENTRY"` with no juggling.
- **Three validators** (the "schema compliance" guarantees):
  - `_force_utc` (`@field_validator("timestamp")`) — every timestamp comes out timezone-aware UTC.
  - `_event_id_is_uuid` — `uuid.UUID(v)` must parse, else reject. Stops a junk id colliding with a
    real one.
  - `_zone_id_rule` (`@model_validator(mode="after")`, runs after the whole object is built) —
    ENTRY/EXIT/REENTRY must have `zone_id=None`; zone/billing events must have one. This is why a
    mislabelled event can never reach the billing-presence logic.
- `IngestResponse` is the 4-way receipt: `received / accepted / duplicates / rejected` + a list of
  per-item `errors`. That split is what lets a caller see **partial success**.

**Gotcha:** a `model_validator(mode="after")` receives `self` (the built model); a
`field_validator` receives just the field value. Mixing them up is a common Pydantic mistake.

---

## `app/config.py` — all tunables in one place
**What:** a `Settings` object whose every value comes from an environment variable (with a default),
cached by `@lru_cache`.

**Public symbols:** `ROOT`, `DATA_DIR`, `Settings`, `get_settings()`.

**Annotated walk:**
- Env vars are read **inside `__init__`** (not as class attributes). That matters: the test suite
  changes `DATABASE_URL` per test and calls `get_settings.cache_clear()`; if the values were
  class-level they'd bind once at import and ignore the change. (We hit exactly this bug and fixed
  it.)
- The numbers you'll reference: `ingest_max_batch=500`, `conversion_window_s=300` (the 5-min rule),
  `min_sessions_for_confidence=20`, `stale_feed_s=600`, `queue_spike_warn/critical=5/8`,
  `dead_zone_s=1800`, `conversion_drop_warn/critical=0.20/0.40`, `anomaly_baseline_days=7`.
  Plus `seed_pos_on_start` / `seed_events_on_start` (auto-seed toggles).

**Gotcha:** `get_settings()` is cached, so it returns the *same* `Settings` object every call. Tests
must `get_settings.cache_clear()` after changing env.

---

## `app/db.py` — the database layer (SQLAlchemy)
**What:** the two tables, the engine, and the timezone normalisation. SQLAlchemy is the library that
maps Python classes ↔ database rows.

**Public symbols:** `Base`, `EventRow`, `PosRow`, `to_naive_utc`, `as_utc`, `DBUnavailable`,
`get_engine`, `init_db`, `reset_engine`, `get_session`, `ping`.

**Annotated walk:**
- `EventRow` = one row per event; `event_id` is `primary_key=True` (a unique index → re-inserting
  the same id is impossible, which is what makes ingest idempotent). `index=True` on the columns we
  filter by (`store_id`, `visitor_id`, `event_type`, `is_staff`, `ts`).
- `PosRow` = one till transaction (`transaction_id` PK).
- `to_naive_utc(dt)` / `as_utc(dt)` — the timezone convention: **strip tz to naive-UTC on write,
  re-attach UTC on read**. Every datetime in the system passes through these, so we never compare a
  tz-aware with a tz-naive datetime (a classic Python `TypeError`).
- `get_session()` is a **generator** used as a FastAPI dependency: it `yield`s a session and, if the
  DB connection fails (`OperationalError`), raises `DBUnavailable` — which `main.py` turns into a
  clean 503. `pool_pre_ping=True` quietly reconnects dropped Postgres connections.
- `_engine`/`_SessionLocal` are module globals built lazily; `reset_engine()` exists so tests can
  point at a fresh SQLite file between cases.

**Gotcha:** `connect_args={"check_same_thread": False}` is only for SQLite (FastAPI may touch the
session from a threadpool). Postgres ignores it.

---

## `app/layout.py` — reading `store_layout.json`
**What:** answers structural questions about the store (which zone is billing, what the product
zones are, open hours). Cached with `@lru_cache`.

**Public symbols:** `load_layout`, `clear_cache`, `store_ids`, `store_config`, `zones`,
`product_zones`, `billing_zone`, `sku_zone`.

**Annotated walk:**
- `product_zones(store)` returns zones whose `type` is `product`/`browse` — i.e. SKIN, MAKEUP,
  HAIRCARE (not ENTRANCE/BILLING/BACKROOM). These are the "Zone Visit" set and the heatmap rows.
- `billing_zone(store)` finds the zone with `type:"billing"`, falling back to a name match
  (BILLING/CHECKOUT/CASH…). Conversion correlation keys off this.
- If the file is missing, returns an empty layout instead of crashing (defensive).

**Gotcha:** cached — `clear_cache()` is called by tests that swap the layout file.

---

## `app/ingestion.py` — `POST /events/ingest` logic
**What:** validate a batch, dedupe, store idempotently, return the 4-way count.

**Public symbols:** `ingest_events(session, raw_events)`, `_row_from_event`, `_persist`.

**Annotated walk (the order of operations *is* the design):**
1. `if received > ingest_max_batch: raise ValueError` → 400. Reject oversize loudly, don't silently
   truncate.
2. Loop each raw dict through `Event.model_validate(raw)`. On failure, append an `IngestItemError`
   with the index + a short reason and **continue** (one bad event doesn't sink the batch). On
   success, dedupe within the batch: `if ev.event_id in valid: in_batch_dups += 1 else: valid[id]=ev`.
3. Query which ids already exist (chunked `IN()` query), count those as `duplicates`, build
   `new_rows` for the rest.
4. `_persist(session, new_rows)` does the insert. Normally one bulk `add_all` + `commit`. If a
   concurrent request inserted the same id between our check and our insert, the commit raises
   `IntegrityError`; we `rollback` and re-insert **one row at a time**, counting collisions as
   duplicates. So a race produces a correct idempotent count, never a 500.
5. `assert received == accepted + duplicates + rejected` — a self-check that every event is
   accounted for exactly once.

**Gotcha:** `event_type=ev.event_type` works because `use_enum_values=True` already made it a plain
string; `_row_from_event` flattens `metadata.queue_depth/sku_zone/session_seq` into their own columns
*and* keeps the full `metadata` dict in the `meta` JSON column.

---

## `app/analytics.py` — the shared core (the most important file on the API side)
**What:** turns rows into **visitor sessions** and runs **POS conversion**. Every read endpoint is a
thin wrapper over this.

**Public symbols:** `now_utc`, `VisitorSession`, `StoreWindow`, `default_window`, `build_window`,
`_any_txn_in`.

**Annotated walk:**
- `VisitorSession` (a `@dataclass`) is the per-person tally: `entered`, `reentered`, `zones_visited`
  (a set), `dwell_by_zone` (max dwell ms per zone), `reached_billing`, `billing_presence_ts` (list of
  times they were at billing), `abandoned`, `max_queue_depth`, `converted`, `min_confidence`.
- `StoreWindow` wraps the dict of sessions + POS totals and exposes the headline properties:
  `unique_visitors = len(sessions)`, `converted_visitors`, and `conversion_rate` (which returns `0.0`
  if there are no sessions — **zero-traffic safe**, no divide-by-zero).
- `default_window(session, store)` decides "today": find the latest event's timestamp, take that
  **day**, and return `[first event that day, latest event + 1s]`. Reasoning is in the docstring —
  it makes the window the *observed operating period*, so purchases and visitors share one basis
  (critical for the 2.5-min clip). Empty store → wall-clock day.
- `build_window(session, store, start, end)` is the workhorse:
  - one SQL query pulls all non-staff events in the window, ordered by time (staff exclusion is the
    single `EventRow.is_staff.is_(False)` clause — the one chokepoint).
  - it loops the rows and folds each into its `VisitorSession` (keyed by `visitor_id`), setting
    `entered` on ENTRY/REENTRY, adding to `zones_visited` for product-zone events, updating
    `dwell_by_zone`, marking `reached_billing` + recording `billing_presence_ts` for billing events,
    and tracking queue depth / abandonment.
  - **conversion:** pull the window's POS rows, `sorted()` their times once, then for each session's
    billing-presence time `pres`, check `_any_txn_in(txn_times, pres, pres+300s)` — if a sale falls
    in that 5-minute window, `s.converted = True`.
- `_any_txn_in` uses `bisect.bisect_left` (binary search) to check the sorted txn list in ~log(n)
  instead of scanning it.

**Gotcha:** dwell is treated as **cumulative** (ZONE_DWELL carries the running total), so we keep the
**max** per (visitor, zone), not the sum. Summing would multiply-count.

---

## The five read endpoints (each ~30 lines, all built on `build_window`)

### `app/metrics.py` — `GET /stores/{id}/metrics`
`compute_metrics` calls `build_window(default_window(...))`, then:
- iterates sessions to compute `avg_dwell_seconds_per_zone` (mean of per-visitor max dwell, ms→s),
  `billing_visitors` (reached billing), `abandonment_rate` (abandoned-and-not-converted ÷ reached
  billing).
- `_current_queue_depth` = the most recent event in the window that carries a `queue_depth`.
- `_pos_today` = the **whole day's** order count + ₹ total (business context), separate from
  the window-scoped `purchases`. This is why the card can show "0 in window / 24 today."
- Returns a flat dict; everything is zero-safe (`is_zero_traffic` flag when no visitors).

### `app/funnel.py` — `GET /stores/{id}/funnel`
`compute_funnel` builds four stage counts **from sessions**:
- `entry = len(s)` (every observed session counts as having entered — this keeps the funnel
  monotonic even when the entry camera misses an arrival), `zone_visit = sessions with any
  zones_visited`, `billing_queue = sessions that reached billing`, `purchase = converted sessions`.
- For each stage it reports `count`, `drop_off_from_previous` (% lost from the prior stage), and
  `pct_of_entry`. Re-entries can't double-count because the unit is the session.

### `app/heatmap.py` — `GET /stores/{id}/heatmap`
`compute_heatmap` tallies, per **product** zone, distinct visitors and average dwell, then
**normalises 0–100** against the busiest zone (so a UI can shade a grid). Emits one cell per product
zone (zero-traffic ones included for a stable grid) and a `data_confidence: "LOW"` flag when the
window has fewer than 20 sessions.

### `app/anomalies.py` — `GET /stores/{id}/anomalies`
`detect_anomalies` runs three detectors and sorts by severity:
- `_queue_spike` — the most recent `queue_depth` in the last 10 min; ≥8 → CRITICAL, ≥5 → WARN.
- `_conversion_drop` — today's conversion vs the average of the last `anomaly_baseline_days` (7)
  days (each computed via `build_window` on that day). Needs ≥2 days with data or it stays silent.
  Relative drop ≥40% → CRITICAL, ≥20% → WARN.
- `_dead_zones` — a product zone with no ZONE_ENTER/DWELL in 30+ min, **gated** by `_is_open`
  (open-hours check via `zoneinfo`, falls back to "open" if no tz db) and only if the store had
  traffic today.
- "Now" = `_store_now` = the store's latest event time, so detectors behave the same live and on
  replay. Every anomaly carries `severity` + a concrete `suggested_action` string.

### `app/health.py` — `GET /health`
`compute_health` first calls `ping()`; if the DB is down it returns `{status: "degraded", db:
"down"}` **without throwing** (you read health *because* something's wrong). Otherwise, per store it
reports `last_event_at` and `lag_seconds` vs **wall-clock now**, flagging `STALE_FEED` past 10 min.
Overall status is `ok` / `warning` (any stale) / `degraded` (db down).

---

## `app/logging_mw.py` — structured request logging
**What:** a Starlette `BaseHTTPMiddleware` that wraps every request and prints one JSON line.

**Annotated walk:** `dispatch` makes a `trace_id` (or reuses an inbound `X-Trace-Id`), times the
request, lets it run, then logs `{trace_id, store_id, endpoint, method, status_code, latency_ms,
event_count}`. `store_id` is parsed from the URL path; `event_count` is set by the ingest route via
`request.state`. The trace_id is echoed back in the response header so a client call ties to a log
line. `configure_logging()` formats the logger to emit the raw JSON message.

---

## `app/main.py` — the FastAPI app (wiring, startup, errors, routes)
**What:** assembles everything.

**Annotated walk:**
- `lifespan` (runs once at startup): `configure_logging()`, `init_db()` (create tables), then
  `_seed_pos` and `_seed_events` — these read `data/pos_transactions.csv` and `data/events.jsonl`
  and load them **if the tables are empty**, so `docker compose up` alone gives a populated
  `/metrics`. Both are wrapped so a bad file logs a warning and never crashes startup.
- `_seed_events` ingests through the **same `ingest_events` path** as a live POST — so seeded data
  is validated identically, no shortcuts.
- **Exception handlers** turn failures into clean JSON (Part C: no stack traces): `DBUnavailable` →
  503, `ValueError` → 400, `SQLAlchemyError` → 503, and a catch-all `Exception` → 500. Each body
  carries the `trace_id`.
- **Routes** are thin: each GET endpoint calls its `compute_*` function with a `Depends(get_session)`
  session. `POST /events/ingest` sets `request.state.event_count` then calls `ingest_events`. The
  dashboard is mounted at `/` (serves `dashboard/index.html`).

**Gotcha:** the catch-all `Exception` handler is last-resort; FastAPI's own `RequestValidationError`
(malformed request body → 422) still takes precedence, so well-formed-but-business-invalid vs
malformed-JSON produce different statuses.

---

# PART B — THE DETECTION PIPELINE (`pipeline/`)

## `pipeline/emit.py` — building & shipping events
**Public symbols:** `make_event(...)`, `JsonlSink`, `HttpSink`.
- `make_event` assembles the dict and runs it through `Event.model_validate` **before it leaves**
  the pipeline — a malformed event fails here, not on ingest. Returns `ev.model_dump(mode="json")`.
- `JsonlSink.emit` writes one JSON line to a file; `HttpSink.emit` buffers and POSTs batches to
  `/events/ingest` (auto-flush at `batch_size`). "Sink" = where events drain.

## `pipeline/tracker.py` — the reasoning layer (pure logic, unit-testable)
**Public symbols:** `appearance_signature`, `_cosine`, `GalleryEntry`, `ReIDGallery`, `LineCrossing`,
`StaffClassifier`, `ZoneMapper`, `_point_in_poly`, `DwellState`.
- `appearance_signature(crop)` → 32-number HSV hue+sat histogram, L1-normalised (the colour
  fingerprint; brightness dropped for lighting tolerance).
- `_cosine(a,b)` → similarity 0–1 (`dot / (‖a‖‖b‖)`).
- `ReIDGallery.assign(camera, track_id, sig, ts)` → `(visitor_id, is_reentry)`. If we've seen this
  `(camera, track_id)` before, reuse and rolling-average the signature (`0.8*old + 0.2*new`).
  Otherwise find the best gallery match within the 15-min TTL; if `≥ 0.72`, reuse that id (and it's a
  re-entry if that visitor had `exited`); else mint a new id. `mark_exit(vid)` flags a visitor as
  gone so a later match becomes a re-entry.
- `LineCrossing.side(pt)` = the signed 2D cross product (which side of the door line); `crossing(prev,
  cur)` returns ENTRY/EXIT/None on a sign flip (see LEARN.md §8.2 for the worked numbers).
- `ZoneMapper.zone_for(pt)` → the polygon (via `_point_in_poly`, ray-casting) containing the point,
  else the camera's single `default_zone`.
- `DwellState` holds per-visitor zone state incl. `pending_zone`/`pending_count` for the
  3-frame hysteresis that stops border jitter.

## `pipeline/detect.py` — the orchestrator (CCTV → events)
**Public symbols:** `run(argv)`, `process_camera(...)`, helpers `_cam_start_fps`, `_centroid`,
`_handle_zone`, `_billing_zone`, `_sku`, `_resolve_clip`.
- `run()` loads the layout, loads YOLO once, then for each camera in the store calls
  `process_camera`. Args: `--store`, `--layout`, `--out` (JSONL) or `--api` (HTTP), `--vid-stride`,
  `--reid-threshold`, `--conf`.
- `process_camera` is the heart (LEARN.md §8.2 annotates the exact loop): `model.track(... stream=
  True, vid_stride=...)` yields one result per frame; for each detected person it does
  crop → signature → `gallery.assign` → centroid → (entry cam) `LineCrossing` with a 3s cooldown
  → (floor/billing) `_handle_zone`. Timestamps = `start_ts + frame_idx/fps`. Constants at top:
  `DWELL_EMIT_S=30`, `CROSS_COOLDOWN_S=3`, `ZONE_CONFIRM_FRAMES=3`.
- `_handle_zone` emits ZONE_ENTER/EXIT/DWELL/BILLING_QUEUE_JOIN, applying the zone-change hysteresis
  via `DwellState.pending_*`.

## `pipeline/run.sh` — one-command runner
Bash wrapper: runs `detect.py` for the store over the layout's `video` paths into `events/`, then
optionally replays into the API; falls back to a "use the simulator" message if no footage.

---

# PART C — DATA & TOOLS

## `data/store_layout.json` — the hand-written calibration (read LEARN.md §8.1)
Per camera: `role`, `covers`, `video`, `fps`, `start` (UTC), and for the entry camera `line_px`,
for the makeup camera `zones_px` polygons. Plus the `zones` dict (`type` = threshold/product/billing
/staff) and `open_hours`. The `floor_plan_zones` block is documentation only (the API ignores it).

## `data/prepare_pos.py` — real POS export → simple POS CSV
`convert()` reads the 101-line-item Purplle export, **dedupes by `order_id`** (one bill, many
products), sums `total_amount` per order for the basket value, parses `order_date + order_time` as
IST and converts to UTC with a **fixed +5:30 offset** (India has no DST and the box has no tz db),
keeps only `invoice_type == sales`, writes `data/pos_transactions.csv` (24 rows).

## `data/generate.py` — synthetic dataset (dev/test fallback only)
Writes to `data/synthetic/` so it can never clobber real data. `plan_day` builds a realistic
arrival/service **queue model** (so queue buildup and abandonment emerge), `visitor_session` emits a
visitor's ordered events, `generate_dataset` writes a multi-day layout + POS + events. Seeded
(`random.Random(7)`) for reproducibility. Only used when there's no footage and to drive the live
simulator's event shapes.

## `tools/simulate.py` — the Part E bridge
`replay_file(api, path)` POSTs a recorded `events.jsonl` into the API in batches (`--rt` paces it by
real time). `live_stream(api, store)` is **layout-driven**: it reads the store's real zones/cameras
and streams freshly-generated visitors stamped at wall-clock now (building a queue, keeping one zone
cold), so `/health` stays fresh and anomalies fire live. This is the stand-in for a live camera.

## `tools/probe_videos.py` — frame extractor
Opens each clip with OpenCV, prints resolution/fps/duration, and saves sample JPGs (used to read the
burned-in clock and eyeball the layout coordinates).

## `tools/verify_e2e.py`, `tools/smoke.py`, `tools/inspect_inputs.py`
- `verify_e2e.py` — boots the API in-process on a temp DB, lets startup auto-seed the real
  events+POS, and prints every endpoint for ST1008 (the end-to-end sanity check).
- `smoke.py` — quick in-process ingest + endpoint check during dev.
- `inspect_inputs.py` — one-off: dumps the layout xlsx sheets and POS-CSV structure (how we learned
  there were 24 orders, the dep_name taxonomy, etc.).

---

# PART D — TESTS (`tests/`)

## `tests/conftest.py` — fixtures
Sets `SEED_POS=0`, `SEED_EVENTS=0`, and `STORE_LAYOUT` → a **fixture** layout (so tests never touch
real data). The `client` fixture makes a fresh temp-SQLite DB per test (`reset_engine` + `init_db`),
yields a `TestClient`. `add_pos` inserts a POS row directly so conversion tests are fully controlled.

## `tests/_helpers.py` — event builders
`mk(event_type, ...)` builds one valid event dict; `visitor(vid, ...)` builds a full ordered event
list for one shopper (entry → zones → optional billing/abandon → exit → optional re-entry);
`billing_time(t0)` computes when that visitor reaches billing (so a POS row can be aligned).
Constants `DAY`, `STORE`, `BILLING`.

## `tests/test_*.py` — what each covers (each has a `# PROMPT:`/`# CHANGES MADE:` block, Part D)
- `test_ingest.py` — idempotency, partial success, in-batch dedup, 400 on oversize, zone_id/uuid
  validation.
- `test_metrics.py` — conversion via staggered POS, staff exclusion, zero-traffic, zero-purchase,
  abandonment, the time-window rule.
- `test_funnel.py` — stage counts + drop-off, re-entry counted once, empty store.
- `test_anomalies.py` — queue spike, dead zone, conversion-drop vs baseline, all-clear.
- `test_health_heatmap.py` — STALE_FEED vs fresh, NO_DATA, heatmap LOW confidence + normalisation,
  product-zones-only.
- `test_pipeline.py` — line-crossing direction, Re-ID re-entry + cross-camera, point-in-polygon,
  zone mapper, `make_event` schema validation (no video needed — hand-made numpy arrays).
- `test_production.py` — DB outage → structured 503, health-when-db-down, dashboard served, POS
  seeding, the concurrent-duplicate `_persist` fallback.

---

# PART E — INFRA & UI

## `Dockerfile` — the API image
`python:3.12-slim`, installs only `requirements.txt` (no CV stack), copies `app/ data/ dashboard/
tools/`, runs as a non-root user, `CMD uvicorn app.main:app`. Small and fast because the heavy
torch/opencv stack lives only in `pipeline/requirements.txt` and never enters this image.

## `docker-compose.yml` — API + Postgres
Two services: `db` (postgres:16-alpine with a healthcheck) and `api` (built from the Dockerfile,
`depends_on db healthy`, `DATABASE_URL` pointing at the db, `./data` mounted read-only). `docker
compose up` = the whole acceptance-gate path with no manual steps.

## `requirements.txt` vs `pipeline/requirements.txt`
The API needs fastapi/uvicorn/sqlalchemy/pydantic/psycopg2 (+ requests for the simulator, numpy for
the pipeline-logic tests). The pipeline additionally needs ultralytics/opencv/scipy/torch — kept
separate so the API container stays ~light.

## `dashboard/index.html` — the live UI (Part E)
A single self-contained file (no build step). Plain JS polls the five GET endpoints every 2s and
renders the North-Star hero, the funnel bars, the ranked zone heatmap, anomalies, and feed health.
OKLCH dark-console tokens, one indigo accent, accessible contrast, `prefers-reduced-motion` honoured.
Key JS functions mirror the endpoints: `renderHero`, `renderFunnel`, `renderHeat`, `renderAnomalies`,
`renderHealth`, driven by `tick()` on a 2s interval.

---

# Call-graphs to trace in the debugger

**An ingest request** (`POST /events/ingest`):
```
main.ingest(payload)                       # app/main.py route
  → request.state.event_count = len(...)   # for the log line
  → ingestion.ingest_events(session, raw)  # app/ingestion.py
      → Event.model_validate(each)         # app/models.py  (validate / reject)
      → select existing event_ids          # app/db.py (dedup)
      → _persist(new_rows)                 # bulk insert, IntegrityError fallback
  → IngestResponse(received, accepted, duplicates, rejected, errors)
logging middleware logs one JSON line with status + latency
```

**A metrics request** (`GET /stores/ST1008/metrics`):
```
main.get_metrics(store_id)                 # app/main.py route, Depends(get_session)
  → metrics.compute_metrics(session, id)   # app/metrics.py
      → analytics.default_window(...)       # pick "today" window
      → analytics.build_window(...)         # rows → VisitorSession dict + POS conversion
          → layout.billing_zone / product_zones   # app/layout.py
          → _any_txn_in (bisect)            # conversion correlation
      → _pos_today, _current_queue_depth    # day context + queue
  → dict → JSON
```

**The pipeline** (per camera):
```
detect.run() → detect.process_camera()      # pipeline/detect.py
  → model.track(stream=True)                 # ultralytics decodes frames
  for each frame, each person:
    → tracker.appearance_signature(crop)     # pipeline/tracker.py
    → tracker.ReIDGallery.assign(...)        # → visitor_id
    → tracker.LineCrossing.crossing(...)     # ENTRY/EXIT  (entry cam)
    → tracker.ZoneMapper.zone_for(...)       # zone        (floor cam)
    → emit.make_event(...) → Sink            # pipeline/emit.py → events.jsonl / API
```

---

*Read a file, then its section here. If any single function is still opaque, name it and I'll trace
it statement by statement with example inputs and outputs.*
