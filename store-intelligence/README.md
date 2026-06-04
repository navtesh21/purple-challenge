# Apex Retail — Store Intelligence

> **Final dataset:** events use the real **three-family wire schema** (entry/zone/queue) + an added
> `visitor_id`, normalized on ingest (`app/schema_compat.py`); **two stores** are supported
> (`ST1008` Brigade Bangalore with POS, `ST2002` Store 2); conversion uses the billing
> `queue_completed`/served signal as well as POS. Schema reference: **[docs/SCHEMA.md](docs/SCHEMA.md)**.

Turn raw retail CCTV into the funnel/conversion analytics an e-commerce team already has.
Raw clips → detection pipeline → one event schema → real-time FastAPI intelligence API → live
dashboard. Built and validated against the real challenge footage: **two Purplle stores** —
**Store 1** (Brigade Road, Bangalore — `ST1008`) with 4 cameras + real POS export for 2026-04-10,
and **Store 2** (`ST2002`) with 4 cameras (two entry points, a floor zone, and billing).

**North Star:** offline store **conversion rate** = converted visitors ÷ unique visitors.

> Want the full reasoning? [`docs/DESIGN.md`](docs/DESIGN.md) (architecture + AI-assisted
> decisions), [`docs/CHOICES.md`](docs/CHOICES.md) (the 3 headline decisions), and
> [`explain.md`](explain.md) (a line-by-line, decision-by-decision tour).

---

## Quick start (5 commands)

```bash
# 1. start the API + Postgres. On boot it auto-seeds the committed, footage-derived
#    events and the real POS log, so /metrics returns real data with NO manual step.
docker compose up --build -d

# 2. confirm the API (real numbers from the real footage)
curl http://localhost:8000/stores/ST1008/metrics

# 3. open the live dashboard
#    http://localhost:8000
#    (Or view the live static snapshot online: https://harlequin-hildegaard-10.tiiny.site/)

# 4. (optional, dashboard bonus) stream events in simulated real-time and watch it move
python tools/simulate.py --api http://localhost:8000 --live --store ST1008

# 5. run the tests + coverage
pytest --cov=app
```

For steps 4–5 (run locally), set up an environment once:

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows  (Linux/mac: . .venv/bin/activate)
pip install -r requirements.txt
```

The API needs only `requirements.txt`. The **detection pipeline** (running YOLO on real video)
additionally needs the CV stack: `pip install -r pipeline/requirements.txt`.

---

## Regenerating events from the CCTV footage

The repo ships `data/events.jsonl` (merged from both stores) — produced by running detection on
the real clips — so the system works out of the box. The footage itself is **not** committed
(challenge-use-only / not redistributable). To reproduce events from raw video:

1. Place the clips in the sub-folders expected by `data/store_layout.json`:

```
../CCTV Footage/
├── Store 1/
│   ├── CAM 1 - zone.mp4        (skincare floor)
│   ├── CAM 2 - zone.mp4        (makeup + haircare floor)
│   ├── CAM 3 - entry.mp4       (glass-door entrance)
│   └── CAM 5 - billing.mp4     (POS / cash counter)
└── Store 2/
    ├── entry 1.mp4              (entry camera 1)
    ├── entry 2.mp4              (entry camera 2)
    ├── zone.mp4                 (display floor)
    └── billing_area.mp4         (billing counter)
```

> Camera-to-role mapping, fps, and per-camera UTC start times are all in
> `data/store_layout.json`. The `video` paths in that file are relative to the layout file,
> so the folder structure above is all that's needed.

2. Run the pipeline for each store:

```bash
pip install -r pipeline/requirements.txt

# Store 1 (Brigade Road, Bangalore — has real POS data)
bash pipeline/run.sh                          # -> events/ST1008.jsonl
# or directly:
python pipeline/detect.py --store ST1008 --layout data/store_layout.json \
    --out data/events_store1.jsonl --vid-stride 5

# Store 2
STORE=ST2002 bash pipeline/run.sh             # -> events/ST2002.jsonl
# or directly:
python pipeline/detect.py --store ST2002 --layout data/store_layout.json \
    --out data/events_store2.jsonl --vid-stride 5
```

`--vid-stride N` processes every Nth frame (CPU tractability) while keeping true timestamps.

### Camera map — Store 1 (ST1008, Brigade Road Bangalore)

| Camera ID | File | Role | Zone(s) |
|---|---|---|---|
| `CAM_ENTRY_01` | `CAM 3 - entry.mp4` | entry/exit threshold (glass door) | ENTRANCE |
| `CAM_SKIN_01` | `CAM 1 - zone.mp4` | floor | SKIN |
| `CAM_MAKEUP_01` | `CAM 2 - zone.mp4` | floor (pixel-polygon split at x=600) | MAKEUP + HAIRCARE |
| `CAM_BILLING_01` | `CAM 5 - billing.mp4` | billing counter | BILLING |

### Camera map — Store 2 (ST2002)

| Camera ID | File | Role | Zone(s) |
|---|---|---|---|
| `CAM_ENTRY_01` | `entry 1.mp4` | entry/exit (horizontal line at y=360) | ENTRANCE |
| `CAM_ENTRY_02` | `entry 2.mp4` | entry/exit (horizontal line at y=360) | ENTRANCE |
| `CAM_FLOOR_01` | `zone.mp4` | floor | DISPLAY |
| `CAM_BILLING_01` | `billing_area.mp4` | billing counter | BILLING |

**Staff detection — per-store uniform colour:** uniform colours differ between stores and are
declared in `data/store_layout.json` under `staff_uniform`. The `StaffClassifier` runs the
appropriate HSV pixel-fraction check for each configured colour; if any exceeds ~20% of the
person crop the person is flagged `is_staff=true` and excluded from all customer metrics.

| Store | Uniform | Check |
|---|---|---|
| ST1008 (Brigade Road) | Red | `red_uniform_fraction` — saturated red pixels (hue 0–10 / 170–180, S ≥ 90, V ≥ 60) |
| ST2002 | Black/dark | `dark_uniform_fraction` — dark, low-saturation pixels (V < 60, S < 60) |

To add a new uniform colour, add a checker function in `pipeline/tracker.py` and register it in
`_UNIFORM_CHECKERS`, then set `"staff_uniform": ["<color>"]` in the store's layout entry. A
`vlm_hook` in `StaffClassifier` is also available for a VLM second opinion on ambiguous crops.

**No footage handy?** `python data/generate.py` writes a synthetic multi-store dataset under
`data/synthetic/`, and `tools/simulate.py --live` streams realistic events — a footage-free
way to exercise the full stack. The detection pipeline emits the identical schema, so nothing
downstream changes.

---

## The API

| Method & path | Returns |
|---|---|
| `POST /events/ingest` | Ingest a batch (≤500): validate, dedup, **idempotent by `event_id`**, partial success with structured per-item errors. |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, purchases + basket value, avg dwell per zone, current queue depth, abandonment. Staff excluded; zero-traffic safe. |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase, by **session**; re-entries counted once. |
| `GET /stores/{id}/heatmap` | Per-zone visit frequency + avg dwell, normalised 0–100, `data_confidence` LOW under 20 sessions. |
| `GET /stores/{id}/anomalies` | Queue spike, conversion drop vs baseline, dead zone — each with severity + `suggested_action`. |
| `GET /health` | Service + DB status, per-store last event timestamp + lag, `STALE_FEED` past 10 min. |
| `GET /stores` | Known store IDs. · `GET /docs` Swagger UI. |

Example: `curl -s http://localhost:8000/stores/ST1008/metrics | jq`

---

## Configuration (env vars; see `app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgresql+psycopg2://…` in compose |
| `SEED_EVENTS` / `SEED_POS` | `1` | auto-seed committed events / POS on first boot |
| `CONVERSION_WINDOW_S` | `300` | billing-presence → POS correlation window |
| `STALE_FEED_S` | `600` | `/health` STALE_FEED threshold |
| `QUEUE_SPIKE_WARN` / `_CRITICAL` | `5` / `8` | queue-depth anomaly thresholds |
| `MIN_SESSIONS_CONF` | `20` | heatmap `data_confidence` floor |

---

## Tests

```bash
pytest --cov=app --cov-report=term-missing
```

37 tests, ~96% statement coverage on `app/`. Edge cases covered: empty store, all-staff clip,
zero purchases, re-entry in the funnel, DB outage → 503, idempotent re-ingest, the
concurrent-duplicate race, oversize batch → 400, schema rules (zone_id/uuid). Each test file
carries the AI prompt block used to draft it.

---

## A note on conversion (engineering judgment)

The provided clips are ~2.5 minutes (~20:10 IST, 2026-04-10). The store made 24 sales that day
(~one per 24 min); the nearest sale to the clip window was ~13 min after it ends, so **no
transaction falls inside the clip's 5-minute correlation window**. The honest, correctly
computed conversion for this clip is therefore ~0 — a footage-coverage limitation, not a logic
bug. The conversion machinery is demonstrated with non-zero values via the live simulator and
targeted tests. See [`docs/CHOICES.md`](docs/CHOICES.md) for the full reasoning.

---

## Layout

```
app/        Intelligence API (FastAPI, SQLAlchemy)
pipeline/   detection: CCTV -> events (YOLOv8 + ByteTrack + histogram Re-ID + StaffClassifier)
tools/      simulate.py (replay/live), probe_videos.py, inspect_inputs.py, smoke.py
data/       store_layout.json, pos_transactions.csv, events.jsonl, prepare_pos.py, generate.py
dashboard/  live web UI
tests/      pytest suite (+ fixtures)
docs/       DESIGN.md, CHOICES.md, SCHEMA.md      explain.md  full walkthrough
```
