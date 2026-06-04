# Apex Retail — Store Intelligence

> **Purplle Tech Challenge 2026 · Round 2**

Turn raw retail CCTV into the funnel and conversion analytics an e-commerce team already has.
Two real Purplle stores processed — **ST1008** (Brigade Road, Bangalore, 4 cameras + real POS) and **ST2002** (Store 2, 4 cameras).

**North Star:** offline store **conversion rate** = converted visitors ÷ unique visitors.

---

## Quick start (2 commands, real data included)

```bash
cd store-intelligence
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** — live dashboard with real footage-derived events auto-seeded.

```bash
# Stream events in real time and watch the dashboard update live
python tools/simulate.py --from-file data/events.jsonl --rt 0.1
```

---

## What's in here

```
store-intelligence/
├── app/          Intelligence API (FastAPI) — metrics, funnel, heatmap, anomalies, health
├── pipeline/     CCTV → events: YOLOv8n + ByteTrack + histogram Re-ID + StaffClassifier
├── dashboard/    Live web UI (polls API every 2s)
├── data/         events.jsonl (real footage events), store_layout.json, POS tools
├── tools/        simulate.py (live replay), probe_videos.py, smoke tests
├── tests/        37 pytest tests, ~96% coverage
└── docs/         DESIGN.md · CHOICES.md · SCHEMA.md
```

Full documentation → **[store-intelligence/README.md](store-intelligence/README.md)**

---

## The system

```
CCTV clips ──► YOLOv8n + ByteTrack ──► behavioural events ──► FastAPI ──► live dashboard
(2 stores)      pipeline/                (JSONL / HTTP)          app/        dashboard/
                                              ▲
                                    real POS export ──────────── conversion correlation
```

- **Re-ID:** HSV histogram gallery with cosine similarity → stable `visitor_id` across re-entries and cameras
- **Staff exclusion:** per-store uniform colour detection on torso crop (red = ST1008, dark = ST2002)
- **Conversion:** `queue_completed` (served at billing) OR billing-presence within 5 min of POS transaction
- **37 tests, ~96% coverage** — idempotent ingest, zero-traffic safety, concurrent-duplicate race, schema enforcement

---

## Docs

| File | What it covers |
|---|---|
| [DESIGN.md](store-intelligence/docs/DESIGN.md) | Architecture, AI-assisted decisions, what breaks at 40 stores |
| [CHOICES.md](store-intelligence/docs/CHOICES.md) | Model selection, schema design, API architecture — options vs LLM suggestion vs final choice |
| [SCHEMA.md](store-intelligence/docs/SCHEMA.md) | Three-family wire schema, boundary normalizer, staff exclusion |
| [explain.md](store-intelligence/explain.md) | Line-by-line walkthrough of every decision |
