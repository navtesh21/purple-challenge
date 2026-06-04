# DESIGN.md

> **Final-dataset update (v2):** events now follow the dataset's real **three-family wire schema**
> (`entry`/`exit`/`reentry`, `zone_entered`/`zone_exited`, `queue_completed`/`queue_abandoned`) with
> an added `visitor_id` linking the families, ingested through a **boundary normalizer**
> (`app/schema_compat.py`); the system runs **two stores** (ST1008 + ST2002); and conversion now
> also uses the billing **`queue_completed` (served)** signal, not POS alone. The authoritative
> schema is **[SCHEMA.md](SCHEMA.md)**. The architecture below is unchanged in spirit (the canonical
> internal record and the analytics are the same); only the data contract at the edges is the new
> wire schema.

Store Intelligence — architecture and the reasoning behind it. The system turns raw CCTV
from **two Purplle stores** — Brigade Road, Bangalore (`ST1008`) and Store 2 (`ST2002`) — into
the same funnel and conversion analytics an e-commerce team already has for the online channel.

**North Star:** offline **conversion rate** = converted visitors ÷ unique visitors.

---

## 1. System at a glance

```
 5 CCTV clips ─► Detection pipeline ─► behavioural events ─► Intelligence API ─► live dashboard
 (CAM 1–5)       (pipeline/)            (one JSON schema)      (FastAPI, app/)     (dashboard/)
                                              ▲                      ▲
                                     real POS export ───────────────┘ (conversion correlation)
```

Four stages, one contract. The **event schema** (`app/models.py`) is imported by both the
pipeline (producer) and the API (consumer), so they can never drift. Everything the API
reports is computed from two tables: `events` and `pos_transactions`.

## 2. The store (from the real data)

Sample frames from each clip (`tools/probe_videos.py`) mapped the cameras to roles, and the
mapping was confirmed against the official floor plans (`Store 1 - layout.png`, `store 2 - layout.png`).

**Store 1 — ST1008, Brigade Road Bangalore** (real POS export, 2026-04-10)

| Camera | File | Role | Zone |
|---|---|---|---|
| `CAM_ENTRY_01` | `CAM 3 - entry.mp4` | entry/exit threshold (glass door) | ENTRANCE |
| `CAM_SKIN_01` | `CAM 1 - zone.mp4` | floor | SKIN |
| `CAM_MAKEUP_01` | `CAM 2 - zone.mp4` | floor (pixel-polygon split at x=600) | MAKEUP + HAIRCARE |
| `CAM_BILLING_01` | `CAM 5 - billing.mp4` | billing counter | BILLING |

**Store 2 — ST2002** (no POS; cameras aligned to one time window)

| Camera | File | Role | Zone |
|---|---|---|---|
| `CAM_ENTRY_01` | `entry 1.mp4` | entry/exit (horizontal line y=360) | ENTRANCE |
| `CAM_ENTRY_02` | `entry 2.mp4` | entry/exit (horizontal line y=360) | ENTRANCE |
| `CAM_FLOOR_01` | `zone.mp4` | floor | DISPLAY |
| `CAM_BILLING_01` | `billing_area.mp4` | billing counter | BILLING |

The clips carry a **burned-in timestamp** (~20:10 IST, 2026-04-10), which `store_layout.json`
records per camera as a UTC `start`, so the pipeline stamps **real wall-clock event times**
that line up with the POS log (also 2026-04-10). The POS export is line-item level; it's
collapsed to **24 orders** with `data/prepare_pos.py` (IST→UTC, basket = Σ line totals).

## 3. Detection pipeline (`pipeline/`)

Per camera: YOLOv8n detects people, ByteTrack assigns per-frame track ids, then the reasoning
layer (`tracker.py`) turns tracks into behaviour:
- **Re-ID gallery** maps unstable track ids → a **stable `visitor_id`** via an HSV-histogram
  appearance signature + cosine similarity within a TTL. A match on a previously-exited
  visitor is a **re-entry** (emitted as `REENTRY`, same id); a match across cameras is
  cross-camera **dedup**.
- **Line crossing** on the entry camera decides ENTRY vs EXIT by which side of the door line
  a centroid moves to — so a group of 3 yields 3 ENTRYs, not 1.
- **Zone mapping** assigns a point to a named zone (per-camera polygons, else the camera's
  covered zone).
- **Staff detection — per-store uniform colour:** `store_layout.json` carries a `staff_uniform`
  list per store (e.g. `["red"]` for ST1008, `["dark"]` for ST2002). `StaffClassifier` runs
  the matching HSV pixel-fraction check against the **torso region** of each crop (rows 25–70%,
  cols 20–80%), excluding hair and legs which are the main false-positive sources — especially
  critical for black uniforms since dark pixels are common in retail scenes. Anyone exceeding
  ~20% uniform-colour coverage is flagged `is_staff=true` and excluded from all customer
  metrics. A `vlm_hook` is available for a VLM second opinion on ambiguous cases.
- Confidence is preserved, never suppressed (brief requirement).

Events are validated against the schema *before* they leave the pipeline and written as JSONL
(or POSTed live). CPU tractability comes from `--vid-stride` (process every Nth frame) while
timestamps still advance by the true inter-frame gap.

## 4. Intelligence API (`app/`)

FastAPI + SQLAlchemy. One shared core (`analytics.py`) collapses events into **visitor
sessions** keyed by `visitor_id`; every endpoint is built on it, so they agree by construction
and staff are excluded in one place.

- `POST /events/ingest` — ≤500/batch, per-item validation (partial success), **idempotent by
  `event_id`**, structured errors; survives concurrent-duplicate races without a 500.
- `GET /stores/{id}/metrics` — unique visitors, conversion, dwell/zone, queue depth,
  abandonment; staff-excluded, zero-traffic safe.
- `GET /stores/{id}/funnel` — Entry→Zone→Billing→Purchase by session (re-entries counted once).
- `GET /stores/{id}/heatmap` — per-zone visits + dwell, normalised 0–100, `data_confidence` flag.
- `GET /stores/{id}/anomalies` — queue spike / conversion-drop-vs-baseline / dead zone, with
  severity + `suggested_action`.
- `GET /health` — per-store last-event lag, `STALE_FEED` past 10 min, DB status.

**Conversion correlation:** there's no `customer_id` in POS, so a visitor counts as converted
if they were in the billing zone within 5 min before a POS transaction (binary-searched over
sorted txn times). Storage is portable (Postgres in compose, SQLite in tests); timestamps are
tz-naive UTC throughout. Production concerns are handled: structured JSON request logs
(`trace_id`, `store_id`, endpoint, latency, event_count, status), DB-down → structured 503
(no stack traces), a catch-all handler, and POS seeding that can't crash startup.

## 5. Live dashboard (`dashboard/`)

A single served page polls every endpoint every 2 s and renders KPIs, the funnel, a zone
heatmap, active anomalies, and feed health — proving the pipeline and API are genuinely
connected. The replay/simulator (`tools/simulate.py`) can backfill a recorded log or stream
fresh events in simulated real-time.

## 6. AI-Assisted Decisions

LLMs were used throughout (model comparison, schema review, test drafting, and an adversarial
multi-agent code audit). Three places they materially shaped the design:

1. **Re-ID approach — I overrode the LLM.** It recommended OSNet learned embeddings "for best
   re-entry accuracy." On a CPU-only box, against 2.5-min clips, that's the wrong altitude, so
   I chose a classical HSV-histogram gallery and left a one-function seam to swap embeddings in
   later. (See CHOICES.md #1.)
2. **`visitor_id` semantics — I overrode the LLM.** It suggested a new id per visit; that would
   re-inflate re-entries, the exact problem the brief asks us to solve. I made `visitor_id` a
   stable per-person token so re-entry dedup is automatic. (CHOICES.md #2.)
3. **Adversarial audit — I agreed and acted.** I ran a 43-agent review of my own code against
   the brief; it confirmed 10 real issues (un-enforced schema rules, a concurrent-ingest race
   → 500, missing exception handlers, a REENTRY-emission gap on non-entry cameras). I fixed all
   of them and added regression tests. The audit notably *rejected* a false "conversion window
   is backwards" claim — a good check that the verification itself was honest.

## 7. What breaks at 40 live stores (and the fix)

1. **Compute-on-read** is fine now (a store-day is a few thousand events) but becomes the
   bottleneck under 40 stores × dashboards polling. Fix: a per-(store, window) TTL cache or an
   ingest-time rollup table.
2. **Idempotency** uses check-then-insert with an `IntegrityError` fallback; under heavy
   concurrency I'd switch to Postgres `INSERT … ON CONFLICT DO NOTHING`.
3. **POS correlation** re-sorts transactions per request; cache sorted txn times per store-day.

These are deliberately deferred, not missed — the North Star framing says optimise only what
makes the metric more accurate or more useful.

## 8. Honest limitation: conversion on a 2.5-min clip

The clip window contained no POS transaction within the 5-minute correlation window (nearest
sale ~13 min later), so the correctly-computed conversion for this clip is ~0. Rather than
fabricate a number (the burned-in timestamps make a time-shift dishonest, and the integrity
check penalises invented outputs), I report the true value, explain it here, and demonstrate
the conversion machinery with non-zero values via the simulator and targeted tests. See
CHOICES.md for the full reasoning.
