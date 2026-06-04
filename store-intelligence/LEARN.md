# LEARN.md — the whole system from scratch, for someone new to ML/CV/analytics

> **Final-dataset update (v2):** the real events come in **three shapes** (entry/exit,
> zone_entered/exited, queue_completed/abandoned) — see **docs/SCHEMA.md**. We emit those exact
> shapes and add a `visitor_id` to link them, and a small "normalizer" (`app/schema_compat.py`)
> turns any shape into one clean internal record. The concepts below (detection → tracking →
> Re-ID → events → sessions → metrics) are unchanged; only the *event shape* is now the three
> families, "billing" is a `queue_completed`/`queue_abandoned` event, and there are two stores.

This guide assumes you can read code and JSON, but have **never done computer vision, machine
learning, or analytics**. It builds every concept from the ground up, then connects each one to
the exact file and function in this repo. Read it top to bottom once; by the end you'll be able
to trace a single shopper from "photons hitting a camera" to "a number on the dashboard."

There is **no math you need to fear here.** Where a formula shows up, it's explained in words
and with a concrete example first.

**Suggested reading order:** §1 → §2 (the CV concepts, the part that's new to you) → §3 (the
event) → §4 (pipeline code) → §5 (the API/analytics) → §6 (end-to-end trace) → §7 (glossary).
Keep `explain.md` for the "why we chose X" interview answers; this file is the "how does it
actually work" textbook.

---

## 1. What problem are we solving, in one paragraph

A retailer (Purplle) knows *everything* about its **website**: how many people visited, what they
looked at, where they dropped off, who bought. In a **physical store** they know almost nothing —
just the till receipts at the end of the day. We point the store's existing **CCTV cameras** at
the problem and reconstruct the same web-style analytics: how many people came in, which sections
they browsed, how long they lingered, where they gave up, and ultimately **what fraction bought
something** (the "conversion rate"). The whole system exists to compute that one number
*accurately* and make it *actionable*.

Two halves, joined by a contract:

```
  REALITY (pixels)                    NUMBERS (analytics)
  ┌───────────────┐   events    ┌──────────────────────┐
  │  CCTV  ──► CV  │ ──JSON────► │  API ──► dashboard    │
  │  pipeline/     │  (contract) │  app/                 │
  └───────────────┘             └──────────────────────┘
```

The "contract" is a fixed JSON shape called an **event**. The left half produces events from
video; the right half consumes events and produces analytics. Neither half needs to know how the
other works — only the event shape. (This is why the same `app/models.py` is imported by both.)

---

## 2. From video to meaning — the computer-vision concepts (the new part)

This is the section that's unfamiliar, so we go slowly. The goal of the pipeline is to convert a
**video** into a list of **events** like "visitor VIS_07 entered the store at 20:10:31" or
"visitor VIS_07 spent 40 seconds in MAKEUP."

### 2.1 What a video actually is

A video is just a **flipbook**: a sequence of still images ("**frames**") shown fast enough to
look like motion. Two numbers describe it:

- **Resolution** — the size of each frame in pixels. Ours is **1080p** = 1920 pixels wide × 1080
tall. A **pixel** is one tiny colored dot; each has a color (in our processing, three numbers
for Blue/Green/Red intensity, 0–255 each).
- **Frame rate (fps)** — frames per second. Ours is ~**30 fps** (CAM 1–3) or ~25 (CAM 4–5). So a
2.5-minute clip is `150 s × 30 ≈ 4,500 frames`.

Key consequence: **frame number ↔ time.** If a clip starts at 20:10:00 and runs at 30 fps, then
frame #900 happened at `20:10:00 + 900/30 = 20:10:30`. That's literally how we timestamp every
event — see `pipeline/detect.py`: `ts = start_ts + timedelta(seconds=frame_idx / fps)`. The clip's
real start time comes from the **timestamp burned into the corner of the footage** (we read it by
eye and recorded it in `store_layout.json` as each camera's `start`).

### 2.2 Object detection — "where are the people?"

Given one frame (one image), we want to know **where the people are**. That job is **object
detection**, done by a pre-trained neural-network **model**. We use **YOLO** ("You Only Look
Once," the `yolov8n.pt` file — `n` = "nano," the small/fast version).

You do **not** train it; it already knows what a person looks like from being trained on millions
of labelled images. You hand it a frame, it hands back a list of **detections**. Each detection is:

- a **bounding box** — four numbers `(x1, y1, x2, y2)` = the top-left and bottom-right pixel
corners of the rectangle around the person,
- a **class** — what it thinks the object is (we only keep class `0` = "person"; `classes=[0]`),
- a **confidence** — a number 0–1 saying how sure it is (0.91 = "very sure it's a person,"
0.35 = "maybe").

Mental model: YOLO is a very good intern who, for each photo, draws rectangles around every person
and writes "person, 0.9 sure" next to each. It does this ~30 times per second.

> **Why confidence matters and why we never throw low ones away:** a blurry, half-hidden shopper
> might come back as 0.4. The brief explicitly says *don't suppress low-confidence detections* —
> dropping them would silently undercount visitors. We keep the number on the event and let the
> analytics decide. (See `confidence` in the schema.)

### 2.3 Tracking — "is this the same person as last frame?"

Detection treats each frame independently. But a shopper appears in *hundreds* of consecutive
frames. We need to know that the person in frame #900 is the *same* person as in frame #901, so we
can follow their path. Linking detections across frames into a **track** is **tracking**.

We use **ByteTrack** (built into YOLO via `model.track(..., tracker="bytetrack.yaml")`). It assigns
each person a **track_id** — a little integer that stays the same frame-to-frame *as long as it can
follow them*. So you get "track 5 is at (400,300), now (405,302), now (410,305)…" — a trail.

Mental model: tracking draws a numbered leash on each box and tries to keep the same number on the
same body as they move.

**The catch (this is important):** track_ids are *fragile*. If a shopper walks behind a shelf for a
second (an **occlusion**), the tracker loses them; when they reappear it often calls them a **new**
track_id. One physical person can rack up several track_ids in one visit. If we counted track_ids
as visitors, we'd massively over-count. That's the problem the next concept solves.

### 2.4 Re-identification (Re-ID) — "have I seen this person before?"

We want a **stable identity per physical person**, not per fragile track. That stable id is our
`**visitor_id`**. Producing it is **Re-ID** ("re-identification").

The idea: when a new track appears, compare its **appearance** to people we've seen recently. If it
looks like someone we already have an id for, reuse that id. Otherwise, mint a new one.

How do you compare "appearance" with code? You turn each person-crop into a **signature** — a short
list of numbers summarizing their colors — and compare signatures.

- **The signature (`appearance_signature` in `tracker.py`):** take the pixels inside the person's
box, convert to **HSV** color space (Hue = which color, Saturation = how vivid, Value =
brightness), and build a **histogram** — basically "how much of each hue/saturation is in this
crop." A person in a red shirt and blue jeans gets a recognizable color fingerprint. We
**deliberately drop the brightness (Value) channel** because lighting changes across the store
(the brief warns about this); colors are more stable than brightness. The result is a list of 32
numbers, scaled so they sum to 1 (a "distribution").
  > This is the simplest honest form of Re-ID. The fancy version uses a second neural network
  > ("embeddings," e.g. OSNet) instead of a color histogram. We chose the histogram because it
  > runs fast on a CPU and is easy to explain; the code leaves a clean seam to swap in embeddings.
- **Comparing two signatures — cosine similarity (`_cosine`):** think of each 32-number signature
as an arrow in space. **Cosine similarity** measures the *angle* between two arrows: `1.0` = same
direction (identical color profile), `0.0` = unrelated. We call it a match when similarity is
above a **threshold (0.72)**. Two people in similar outfits can falsely match — that's a real
limitation we document; a stricter threshold reduces false matches but then track fragmentation
can split one person into two. There's no free lunch; we pick a sensible middle.
- **The gallery (`ReIDGallery`):** a little memory of recently-seen people: `{visitor_id → signature, last_seen_time, exited?}`. For each new track:
  1. compute its signature,
  2. find the most-similar gallery entry seen within the last **15 minutes** (the "TTL," time-to-live),
  3. if similarity ≥ 0.72 → reuse that `visitor_id`. If that entry had already **exited**, this is a
    **re-entry** (they left and came back — same person, new visit). If across a different camera,
     it's **cross-camera dedup** (same person seen by two cameras, counted once).
  4. otherwise → mint a brand-new `visitor_id`.

That single mechanism solves three of the brief's edge cases at once: re-entry inflation,
cross-camera double-counting, and track fragmentation.

### 2.5 Geometry — turning pixel positions into meaning

Now we know *who* (visitor_id) is *where* (a box) in each frame. Two geometric tricks turn position
into behavior.

- **Centroid:** the center point of a box, `((x1+x2)/2, (y1+y2)/2)`. We track the centroid as "where
the person is."
- **Entry vs exit — line crossing (`LineCrossing`):** at the door we draw an imaginary **line** (two
points, stored as `line_px` in the layout). For any point we can ask "which **side** of the line
is it on?" using a tiny bit of vector math called the **2D cross product** — the formula returns a
positive number on one side, negative on the other. We watch a visitor's centroid frame to frame;
when the sign **flips** (positive→negative or vice-versa), they **crossed** the line. The
*direction* of the flip tells us **ENTRY** (walked into the store) vs **EXIT** (walked out). This
is why a **group of 3** people produces **3 ENTRY events** — three separate tracks each cross the
line — not one "group" blob.
  > Real-world wrinkle we hit: people loitering near the glass door make the centroid jiggle across
  > the line repeatedly, producing dozens of fake ENTRY/EXIT events. Fix: a 3-second **cooldown**
  > per visitor (`CROSS_COOLDOWN_S`) so we only count one crossing per few seconds.
- **Which section are they in? — zones & point-in-polygon (`ZoneMapper`):** the store floor is
divided into named **zones** (SKIN, MAKEUP, HAIRCARE, BILLING…). On a camera, a zone is a
**polygon** — a list of pixel corners outlining a region (stored as `zones_px`). To decide a
visitor's zone, we ask "is their centroid **inside** this polygon?" using the classic
**ray-casting** algorithm (`_point_in_poly`): shoot an imaginary ray to the right from the point
and count how many polygon edges it crosses — odd = inside, even = outside. For CAM 2 we split the
frame at x=600 into MAKEUP (right) and HAIRCARE (left). If a camera has no polygons, everyone it
sees is in that camera's single zone (e.g. CAM 1 = all SKIN).
  > Same jitter problem at zone borders → same fix: a visitor must be seen in a new zone for **3
  > consecutive frames** (`ZONE_CONFIRM_FRAMES`) before we switch their zone. This "hysteresis" is
  > why the event count dropped from a noisy ~1000 to a clean ~90.
- **Dwell:** "how long were they in this zone." We record when they entered a zone and emit a
`ZONE_DWELL` event **every 30 seconds** they stay (`DWELL_EMIT_S`), with the running total in
`dwell_ms` (milliseconds). That's how the heatmap later knows "people linger 110s in SKIN."

### 2.6 Putting 2.1–2.5 together: the pipeline's job

For each camera clip, for each frame, for each detected person:
**detect (box+confidence) → signature → gallery → stable visitor_id → centroid → zone/line →
behavior → emit an event.** Multiply by 4,500 frames × 5 cameras and you get a stream of a few
dozen clean behavioral events. That stream is the only thing the analytics side ever sees.

---

## 3. The event — the bridge between the two halves

An **event** is one small JSON record describing one thing that happened. This is *the* central
data structure; everything upstream produces them and everything downstream consumes them. Defined
once in `app/models.py` (using **Pydantic**, a Python library that validates that JSON matches a
declared shape).

```jsonc
{
  "event_id":   "3118dd8b-…-c2bd",   // globally unique id (a UUID). Used to dedupe.
  "store_id":   "ST1008",            // which store
  "camera_id":  "CAM_ENTRY_01",      // which camera produced it
  "visitor_id": "VIS_000007",        // the STABLE person id from Re-ID (§2.4)
  "event_type": "ENTRY",             // one of 8 allowed kinds (see below)
  "timestamp":  "2026-04-10T14:40:31Z", // when, in UTC (§2.1)
  "zone_id":    null,                // which zone (null for ENTRY/EXIT, which are at the door)
  "dwell_ms":   0,                   // how long in this zone, milliseconds
  "is_staff":   false,               // staff are excluded from customer metrics
  "confidence": 0.88,                // the detection confidence (§2.2), kept honestly
  "metadata":   { "queue_depth": null, "sku_zone": null, "session_seq": 1 }
}
```

**The 8 event types** (`EventType` enum) — the verbs of a shopping trip:


| event_type              | meaning                                                    |
| ----------------------- | ---------------------------------------------------------- |
| `ENTRY`                 | crossed the door inward (starts a visit)                   |
| `EXIT`                  | crossed the door outward                                   |
| `REENTRY`               | a previously-exited person came back (same visitor_id)     |
| `ZONE_ENTER`            | walked into a product zone                                 |
| `ZONE_EXIT`             | left a product zone                                        |
| `ZONE_DWELL`            | still in a zone (emitted every 30s, carries running dwell) |
| `BILLING_QUEUE_JOIN`    | reached the billing area (carries `queue_depth`)           |
| `BILLING_QUEUE_ABANDON` | left the billing area without buying                       |


**Why a strict schema matters:** Pydantic *rejects* anything malformed — a bad `event_type`, a
`confidence` above 1, a non-UUID id, a `zone_id` set on an ENTRY (which must be null). Bad data is
caught at the door instead of corrupting a metric three steps later. The pipeline validates each
event *before sending it* (`emit.py`'s `make_event`), and the API validates again *on receipt*
(`ingestion.py`). Belt and suspenders.

---

## 4. The pipeline code, walked through (`pipeline/`)

Three files. Read them in this order.

### 4.1 `emit.py` — building and shipping events

- `make_event(...)` assembles the JSON dict and runs it through the `Event` schema (raises if
invalid). One function = the only way an event is born, so nothing malformed escapes.
- `JsonlSink` writes events to a file (`events.jsonl`, one JSON per line). `HttpSink` POSTs them to
the API in batches. "Sink" = where events drain to. Batch mode for offline processing, HTTP mode
for live streaming — same events either way.

### 4.2 `tracker.py` — the reasoning helpers (all of §2.4–2.5 live here)

`appearance_signature`, `_cosine`, `ReIDGallery`, `LineCrossing`, `ZoneMapper`, `_point_in_poly`,
`DwellState`. Pure logic, no video decoding — which is why it's unit-testable without footage
(`tests/test_pipeline.py` feeds it hand-made numbers).

### 4.3 `detect.py` — the orchestrator

`run()` reads `store_layout.json`, loads YOLO once, then for each camera calls `process_camera`,
which is the heart:

```text
for each frame in the clip:
    ts = clip_start + frame_number / fps          # §2.1 timestamp
    for each detected person (box, track_id, confidence):
        crop      = the pixels inside the box
        signature = appearance_signature(crop)     # §2.4
        visitor_id, is_reentry = gallery.assign(camera, track_id, signature, ts)
        centroid  = center of the box              # §2.5
        if this is the entry camera:
            cross = line.crossing(prev_centroid, centroid)   # ENTRY / EXIT / None
            emit ENTRY/REENTRY/EXIT accordingly (with the 3s cooldown)
        else (floor / billing camera):
            zone = zone_mapper.zone_for(centroid)            # which section
            _handle_zone(...) → emits ZONE_ENTER / ZONE_DWELL / ZONE_EXIT / BILLING_QUEUE_JOIN
                                (with the 3-frame confirm so borders don't flicker)
```

`_handle_zone` keeps a tiny `DwellState` per visitor (current zone, when they entered it, the
pending-zone counter) and decides which zone events to emit. The back-room camera (CAM 4) forces
`is_staff=true` for everyone it sees — a clean, honest staff signal because only staff go in the
stock room.

The output is `data/events.jsonl`. **That file is the entire deliverable of the left half.** From
here on, nothing knows or cares that a camera ever existed.

---

## 5. From events to analytics (`app/`) — the part you build dashboards on

### 5.1 What "the API" is

A small web server (**FastAPI**) that other programs talk to over HTTP. It exposes **endpoints** —
URLs that return JSON. `POST /events/ingest` *accepts* events; `GET /stores/ST1008/metrics`
*returns* computed numbers. The dashboard is just a web page that calls those GET endpoints every 2
seconds and draws the results.

### 5.2 Storage (`db.py`) — two tables

A **database** stores rows in **tables**. We use only two:

- `**events`** — one row per event (exactly the schema above). `event_id` is the **primary key**
(a unique index), which is what makes re-sending the same event harmless.
- `**pos_transactions`** — one row per real till sale: `store_id, transaction_id, timestamp, basket_value_inr`.

Everything else (sessions, funnels, conversion) is **computed on the fly** when asked, by reading
these rows. We deliberately store **no pre-computed metrics** — they'd go stale; recomputing from a
few thousand rows takes milliseconds.

> **One subtle, important detail — time zones.** The store is in India (UTC+5:30). The footage clock
> and the POS log are local time; we convert everything to **UTC** and store timestamps without a
> timezone label ("tz-naive UTC"). Mixing "aware" and "naive" timestamps is a classic Python crash;
> normalizing everything to one convention (`to_naive_utc` on write, `as_utc` on read) avoids it.

### 5.3 The POS data and what "conversion" means

The till export (`prepare_pos.py`) is **line-item level**: one row per product. 101 rows, but many
rows share an `order_id` (one bill = many products). A **transaction** is a *bill*, not a product,
so we collapse to **24 orders** (sum each order's line totals → basket value, convert IST→UTC).

There is **no customer name** in the POS data (privacy), so we can't directly say "visitor VIS_07
bought order #441." Instead the brief defines conversion by **time + place**:

> A visitor who was in the **billing zone** within the **5 minutes before** a transaction's
> timestamp counts as having converted.

### 5.4 The analytics core (`analytics.py`) — sessions, the one idea everything reuses

`build_window(store, start, end)` reads all (non-staff) events in a time window and **groups them by
`visitor_id`** into `VisitorSession` objects. One session = one physical person's behavior. For each
session it tallies: did they enter, which zones they visited, max dwell per zone, did they reach
billing and *when* (the "billing-presence timestamps"), did they abandon, the queue depth they saw.

Then **conversion correlation**:

- collect every billing-presence timestamp across all sessions,
- pull the store's POS transaction times in the window, **sort them once**,
- for each billing presence at time `T`, ask "is there a sale between `T` and `T+5min`?" using
**binary search** (`bisect`) — a way to check a sorted list in ~log(n) steps instead of scanning
all of it. If yes, that visitor is **converted**.

`conversion_rate = converted_visitors ÷ unique_visitors`. If there are zero visitors it returns
`0.0` (never a divide-by-zero or `null`). Because sessions are keyed by `visitor_id`, a re-entering
shopper is **one** session — re-entry can't inflate the number. That's the whole reason §2.4 exists.

`default_window` decides what "today" means: it anchors to the **most recent event's day** and spans
**first event of that day → last event** — so the same code is correct whether you're watching a
live feed (latest event = now) or replaying historical footage (latest event = that clip's day).

### 5.5 The five read endpoints — each is a thin wrapper over sessions

- `**metrics.py` → `/metrics*`* — the headline card: unique visitors, conversion rate, purchases +
basket value (and the day's total sales for context), average dwell **per zone**, current queue
depth, abandonment rate, a `is_zero_traffic` flag. All zero-safe.
- `**funnel.py` → `/funnel`** — the drop-off story as **counts of sessions** at four stages:
`Entry → Zone Visit → Billing Queue → Purchase`, with the % lost between stages. Answers "where do
we lose people?" Counting *sessions* (not events) is why re-entries don't double-count.
- `**heatmap.py` → `/heatmap`** — per product zone: how many distinct visitors and their average
dwell, each **normalized 0–100** (the busiest zone = 100) so a UI can shade a grid. Flags
`data_confidence: LOW` under 20 sessions so you don't over-read a tiny sample.
- `**anomalies.py` → `/anomalies`** — three watchdogs, each with a severity and a suggested action:
**queue spike** (recent queue_depth past 5/8), **conversion drop** (today vs a trailing 7-day
average; needs ≥2 days of history so it doesn't cry wolf on day one), **dead zone** (a section
with no visits for 30+ min during open hours).
- `**health.py` → `/health`** — the on-call view: is the DB up, and per store, how long since the
last event (`STALE_FEED` if >10 min). Stays responsive even when the DB is down.

### 5.6 Ingestion & production glue (`ingestion.py`, `main.py`, `logging_mw.py`)

- `**/events/ingest**` accepts up to 500 events at once, validates each independently (one bad event
doesn't sink the batch → "partial success"), and is **idempotent**: re-sending the same events
changes nothing (they collide on `event_id`). It returns a 4-way count: received / accepted /
duplicates / rejected.
- `**main.py`** wires it together and, on startup, **auto-seeds** the committed `events.jsonl` and
POS CSV so `docker compose up` alone gives you a working `/metrics` with no manual step. It also
turns failures into clean JSON errors (DB down → HTTP 503, never a raw stack trace).
- `**logging_mw.py`** prints one JSON line per request (`trace_id, store_id, latency_ms, …`) — what
an ops team needs to debug.

---

## 6. End-to-end trace: one shopper, camera to dashboard

Follow visitor **VIS_07**, who walks in, browses makeup, and leaves:

1. **20:10:31** — VIS_07 steps through the door. CAM 3 (entry) detects a person (box, conf 0.88).
  Gallery sees a new appearance → mints `VIS_07`. Their centroid crosses the door line inward →
   `**ENTRY`** event, timestamp `20:10:31Z`. Written to `events.jsonl`.
2. **20:10:55** — On CAM 2, VIS_07's centroid is on the right (x>600) → MAKEUP zone, seen for 3
  straight frames → `**ZONE_ENTER`** (zone=MAKEUP).
3. **20:11:25** — Still in MAKEUP → `**ZONE_DWELL`** with `dwell_ms≈30000`.
4. **20:11:40** — Walks to the door, centroid crosses outward → `**EXIT`**.
5. The pipeline finishes; `events.jsonl` now has VIS_07's 4 events among ~90 total.
6. `docker compose up` → the API ingests all events (validated, stored in the `events` table) and
  the POS CSV.
7. Dashboard calls `GET /stores/ST1008/metrics`. The API runs `build_window`, which groups VIS_07's
  4 events into **one session**: entered=yes, zones={MAKEUP}, reached_billing=no. They never hit
   billing, so they can't be "converted." They count as **1 unique visitor**, contribute to the
   **MAKEUP** heatmap cell, and sit at the **Zone Visit** stage of the funnel (then drop off before
   Billing).
8. The dashboard draws: unique visitors +1, MAKEUP gets a visit, the funnel shows the Entry→Zone→
  Billing drop. All within ~2 seconds of the poll.

That's the entire system: photons → boxes → a stable id → behavioral events → grouped sessions →
metrics → pixels on a screen.

---

## 7. Glossary (every term, one line each)

- **Frame** — one still image in the video. **fps** — frames per second.
- **Resolution / pixel** — frame size in dots; a pixel is one colored dot (B,G,R values 0–255).
- **Model** — a pre-trained neural network you give an input and get a prediction from. **YOLO** —
the person-detection model we use.
- **Detection** — one box + class + confidence for one object in one frame.
- **Bounding box** — the rectangle `(x1,y1,x2,y2)` around an object. **Centroid** — its center.
- **Confidence** — 0–1, how sure the model is.
- **Tracking / track_id** — linking the same object across frames; the (fragile) per-clip id.
- **ByteTrack** — the tracking algorithm we use.
- **Occlusion** — when something blocks the view of a person (breaks tracking).
- **Re-ID** — re-identification: giving the same physical person a stable id across tracks/cameras.
- **visitor_id** — our stable per-person id (the output of Re-ID).
- **Appearance signature / histogram** — a short list of numbers summarizing a crop's colors.
- **HSV** — a color representation: Hue (which color), Saturation (vividness), Value (brightness).
- **Cosine similarity** — 0–1 measure of how alike two signatures are (angle between them).
- **Gallery** — the memory of recently-seen visitors and their signatures.
- **TTL** — time-to-live; how long a gallery entry stays matchable (15 min here).
- **Line crossing / cross product** — math to tell which side of the door line a point is on, and
thus ENTRY vs EXIT direction.
- **Zone / polygon / point-in-polygon** — a named store section; its pixel outline; the test for
"is this point inside that outline."
- **Hysteresis / cooldown** — requiring a change to persist before acting, to kill jitter.
- **Dwell** — time spent in a zone.
- **Event** — one JSON record of one thing that happened; the contract between halves.
- **Schema** — the declared shape/rules an event must match. **Pydantic** — the library enforcing it.
- **Enum** — a fixed set of allowed values (our 8 event types).
- **UUID** — a globally-unique id string (the `event_id`).
- **API / endpoint / REST** — a web server and its callable URLs returning JSON.
- **FastAPI / Starlette / Uvicorn** — the Python web framework, its core, and the server that runs it.
- **Database / table / row / primary key** — where data is stored; a primary key is a unique column.
- **SQLAlchemy** — the library that talks to the database from Python. **Postgres / SQLite** — the
database engines (production / tests).
- **Idempotent** — doing it twice has the same effect as once (re-sending events is safe).
- **Ingest** — accepting and storing incoming events.
- **Session** — all of one visitor_id's events, grouped; the unit of all analytics.
- **POS** — point of sale (the till); **transaction/order** — one bill; **line item** — one product.
- **Conversion (rate)** — fraction of visitors who bought = converted ÷ unique visitors.
- **Correlation window** — the 5-minute "billing presence before a sale" rule linking visits to sales.
- **Binary search / bisect** — fast lookup in a sorted list.
- **Funnel** — Entry→Zone→Billing→Purchase counts showing where people drop off.
- **Heatmap** — per-zone visit/dwell intensities, normalized 0–100.
- **Anomaly** — an automatically-detected operational problem (queue spike, conversion drop, dead zone).
- **STALE_FEED** — a camera/store hasn't sent events recently (health warning).
- **Docker / docker compose / container / image** — packaging so the app runs identically anywhere.
- **Synthetic data** — fabricated-but-realistic events used only for tests/demo when no footage is present.

---

## 8. Appendix — reading the actual code (your specific questions)

This appendix shows the *real* code (copied verbatim from the files) with line-by-line notes, and
answers three things directly: **how the layout/coordinates were defined**, **how a video becomes
frames that get processed one by one**, and **how this would run in real time**.

### 8.1 How the store layout was defined — yes, by hand-written pixel coordinates

There is **no automatic layout detection.** I looked at sample frames and **typed pixel
coordinates** into `data/store_layout.json`. Here's exactly how.

**The coordinate system.** Every frame is a grid of pixels. The origin `(0,0)` is the **top-left**
corner. `x` increases to the **right** (0 … 1919), `y` increases **downward** (0 … 1079). So
`(960, 540)` is the middle of a 1920×1080 frame. A "box" `(x1,y1,x2,y2)` is top-left + bottom-right
corners. Everything geometric in the system speaks this one coordinate language.

**Step 1 — get pictures to look at.** `tools/probe_videos.py` opens each clip and saves a few JPG
frames (and prints resolution/fps/duration):

```python
cap = cv2.VideoCapture(str(path))          # open the video file
fps = cap.get(cv2.CAP_PROP_FPS)            # e.g. 29.97
n   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))  # total frames, e.g. 4193
for pct in [0.02, 0.25, 0.5, 0.75, 0.98]:  # sample at 2%, 25%, 50%, ...
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * pct))  # jump to that frame
    ok, frame = cap.read()                 # decode it into a pixel array
    cv2.imwrite(out_path, frame)           # save as a .jpg I can open and eyeball
```

I opened those JPGs, saw the burned-in clock (`10/04/2026 20:10:27`), saw which wall each camera
faced (skincare brands, makeup brands, the cash counter, the stock room, the glass door), and
**measured by eye** where the door line and the zone split fall in pixels.

**Step 2 — write the coordinates into the layout.** Here is the real `CAM_ENTRY_01` (the door) and
`CAM_MAKEUP_01` (the split floor) from `store_layout.json`:

```jsonc
"CAM_ENTRY_01": {
  "role": "entry",
  "covers": ["ENTRANCE"],
  "video": "../../CCTV Footage/CAM 3.mp4",
  "fps": 29.97,
  "start": "2026-04-10T14:40:00Z",        // UTC start time, read off the burned-in clock
  "line_px": [[1250, 0], [1250, 1080]]     // the DOOR LINE: a vertical line at x=1250,
}                                          // from top (y=0) to bottom (y=1080).
                                           // store interior is LEFT (x<1250), corridor RIGHT.

"CAM_MAKEUP_01": {
  "role": "floor",
  "covers": ["MAKEUP", "HAIRCARE"],
  "fps": 29.97,
  "start": "2026-04-10T14:40:02Z",
  "zones_px": {                            // two RECTANGLES that tile the 1920×1080 frame:
    "HAIRCARE": [[0,0],[600,0],[600,1080],[0,1080]],     // left third  (x: 0→600)
    "MAKEUP":   [[600,0],[1920,0],[1920,1080],[600,1080]] // the rest    (x: 600→1920)
  }
}
```

So the answers to "did you provide it the coordinates?":
- **Door line** = two points `[[1250,0],[1250,1080]]` → a vertical line at x=1250. I picked 1250 by
  looking at the entry frame and seeing the glass door sits about two-thirds across.
- **Zones** = polygons (here, simple rectangles) under `zones_px`. I split CAM 2 at **x=600**
  because in the frame the hair/accessories run is on the left and the makeup wall on the right.
- **`start` / `fps`** = the wall-clock time and frame rate per camera, so frame-number can be turned
  into a real timestamp (§2.1).

A camera with **no** `zones_px` (e.g. CAM 1) just maps everyone it sees to its single `covers` zone
(SKIN). That fallback is the `default_zone` you'll see in `ZoneMapper` below.

> In a production product you'd never hand-type these. You'd ship a small calibration UI where a
> store manager draws the door line and zone boxes on a still frame with the mouse, and it writes
> these same `line_px` / `zones_px` numbers. The data shape is identical; only the input method
> changes.

### 8.2 How a video is processed — frame by frame (your "divides into frames and checks each frame")

You don't manually split the video. **ultralytics + OpenCV** decode it for you, one frame at a time,
and hand each decoded frame straight into YOLO. The whole engine is this call in
`pipeline/detect.py`:

```python
results = model.track(source=str(clip_path), stream=True, persist=True,
                      tracker="bytetrack.yaml", classes=[0], conf=conf_threshold,
                      verbose=False, vid_stride=vid_stride)
```

What each argument means:
- `source` — the video file (or, for live, a camera URL — see §8.3).
- `stream=True` — **return a lazy generator**: it decodes and yields **one result per frame** as you
  loop, instead of loading the whole 4,500-frame video into memory. This is the "divide into frames"
  part — OpenCV pulls the next frame each iteration.
- `persist=True` — keep the tracker's memory **across** frames (so track_ids stay consistent); without
  it, every frame would be treated fresh.
- `tracker="bytetrack.yaml"` — use ByteTrack for the cross-frame linking (§2.3).
- `classes=[0]` — only detect **person** (COCO class 0); ignore chairs, bags, etc.
- `conf=0.25` — keep detections with confidence ≥ 0.25.
- `vid_stride=5` — only run detection on **every 5th frame** (5–6 fps instead of 30) so it finishes
  on a CPU in minutes. The timestamp math accounts for the skip.

Then the loop — this is the "check each frame" part, annotated:

```python
for step, res in enumerate(results):          # res = one decoded+detected frame
    frame_idx = step * vid_stride              # real frame number (we skipped some)
    ts = start_ts + timedelta(seconds=frame_idx / fps)   # → real timestamp (§2.1)
    if res.boxes is None or res.boxes.id is None:
        continue                               # no people this frame → skip
    frame = res.orig_img                       # the raw pixels: a numpy array, shape (1080,1920,3)

    # res.boxes holds ALL people found this frame, as parallel arrays:
    for box, tid, det_conf in zip(res.boxes.xyxy.tolist(),   # box corners [x1,y1,x2,y2]
                                  res.boxes.id.int().tolist(),# ByteTrack track_id
                                  res.boxes.conf.tolist()):   # confidence 0–1
        x1, y1, x2, y2 = [int(v) for v in box]
        crop = frame[max(0,y1):y2, max(0,x1):x2]   # the person's pixels: numpy slice of the image
        sig  = T.appearance_signature(crop)         # → 32-number colour fingerprint (§2.4)
        vid, is_reentry = gallery.assign(camera, tid, sig, ts)  # → stable visitor_id
        c = _centroid(box)                          # ((x1+x2)/2, (y1+y2)/2)
        ...                                         # line-cross / zone logic → emit events
```

A few things worth seeing close up:

- **`frame = res.orig_img` is a numpy array** of shape `(height, width, 3)` — 1080 rows × 1920
  columns × 3 colour channels. `frame[y1:y2, x1:x2]` is ordinary array slicing: rows `y1..y2`
  (vertical), columns `x1..x2` (horizontal) — i.e. crop out the rectangle around the person. (Note
  rows = y first, then columns = x; that's numpy's order.)

- **The colour fingerprint** (`tracker.py`), with the cv2 calls that build the histogram:
  ```python
  def appearance_signature(crop):
      hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)        # BGR pixels → Hue/Sat/Value
      hist = cv2.calcHist([hsv], [0,1], None, [16,2], [0,180,0,256])  # 16 hue × 2 sat buckets
      hist = hist.flatten().astype(np.float32)            # → 32 numbers
      s = hist.sum()
      return hist / s if s > 0 else hist                  # scale so they sum to 1
  ```
  `calcHist` counts how many pixels fall into each of the 16×2 = 32 colour buckets. That count
  vector *is* the person's colour signature.

- **The Re-ID matching** (`ReIDGallery.assign`), the heart of "have I seen this person?":
  ```python
  # try to match an existing (possibly exited) visitor
  best_vid, best_sim = None, 0.0
  for vid, e in self._gallery.items():
      if ts - e.last_seen > self.ttl:        # skip anyone not seen in 15 min
          continue
      sim = _cosine(sig, e.signature)        # how similar are the colour fingerprints?
      if sim > best_sim:
          best_vid, best_sim = vid, sim
  if best_vid is not None and best_sim >= self.sim_threshold:   # 0.72
      is_reentry = e.exited                  # matched someone who had left → re-entry
      ...
      return best_vid, is_reentry
  vid = self._new_visitor(sig, ts)           # nobody matched → brand-new person
  return vid, False
  ```

- **ENTRY vs EXIT** (`LineCrossing`), with the actual math and a worked number:
  ```python
  def side(self, pt):                        # which side of the door line is this point?
      (x1,y1),(x2,y2) = self.p1, self.p2
      x,y = pt
      return (x2-x1)*(y-y1) - (y2-y1)*(x-x1) # 2D cross product: + one side, − the other

  def crossing(self, prev, cur):
      a, b = self.side(prev), self.side(cur)
      if a==0 or b==0 or (a>0)==(b>0):       # same side both frames → no crossing
          return None
      going_positive = b > 0
      inbound = going_positive == self.inbound_positive
      return "ENTRY" if inbound else "EXIT"
  ```
  Worked example with our door line `p1=(1250,0), p2=(1250,1080)`:
  `side(x,y) = (1250-1250)*(y) − (1080)*(x−1250) = −1080·(x−1250)`.
  - A point in the corridor `x=1300` → `−1080·(50) = −54000` (negative side).
  - A point inside the store `x=1200` → `−1080·(−50) = +54000` (positive side).
  - Someone walking corridor→store goes from `a<0` to `b>0`: the sign flips, `going_positive=True`,
    `inbound=True` → **ENTRY**. Walking the other way → **EXIT**. That's the whole entry/exit logic.

- **Which zone** (`ZoneMapper` + ray-casting), the actual point-in-polygon:
  ```python
  def zone_for(self, pt):
      if not self.polygons:        return self.default_zone   # no polygons → camera's one zone
      for zone, poly in self.polygons.items():
          if _point_in_poly(pt, poly): return zone
      return self.default_zone

  def _point_in_poly(pt, poly):    # shoot a ray to the right; odd #crossings = inside
      x,y = pt; inside = False; n = len(poly)
      for i in range(n):
          x1,y1 = poly[i]; x2,y2 = poly[(i+1)%n]
          if (y1>y) != (y2>y):                                  # edge straddles our row
              xin = (x2-x1)*(y-y1)/(y2-y1+1e-9) + x1            # where the edge crosses row y
              if x < xin: inside = not inside                   # ray passes this edge
      return inside
  ```
  For CAM 2, a centroid at `x=300` lands inside the HAIRCARE rectangle (x: 0→600); at `x=1000` inside
  MAKEUP (x: 600→1920). That single `x<600?` decision is the whole sub-zoning.

After this, `_handle_zone` turns "they're in MAKEUP now" into ZONE_ENTER / ZONE_DWELL / ZONE_EXIT
events (with the 3-frame confirm so a jittery centroid at the x=600 border doesn't spam events), and
every emitted event flows through `make_event` → the sink → `events.jsonl`.

### 8.3 How it runs in real time (your "how will you do it in real time")

Right now the pipeline runs in **batch**: read a finished `.mp4` → produce `events.jsonl` → replay
that file into the API. Real-time is the *same loop* with two changes.

**Change 1 — the source is a live stream, not a file.** ultralytics' `model.track` accepts an RTSP
URL (what IP cameras speak), an HTTP stream, or a webcam index — anything OpenCV can open:

```python
# batch (today):
model.track(source="../CCTV Footage/CAM 3.mp4", stream=True, ...)
# live (one-line change):
model.track(source="rtsp://192.168.1.50:554/stream1", stream=True, ...)
```

With a live source, the generator simply **never ends** — it yields a result for each new frame as
the camera produces it, and the exact same `for step, res in results:` loop runs forever.

**Change 2 — timestamps come from the wall clock, not clip-start+offset.** In batch we compute
`ts = start_ts + frame_idx/fps`. Live, each event is stamped `datetime.now(timezone.utc)` because the
frame *is* happening now.

**Shipping events as they happen — `HttpSink` (`pipeline/emit.py`):**

```python
class HttpSink:
    def __init__(self, base_url, batch_size=200):
        self.url = base_url.rstrip("/") + "/events/ingest"
        self._buf = []
    def emit(self, event):
        self._buf.append(event)
        if len(self._buf) >= self.batch_size:
            self.flush()                       # POST the batch to the API
    def flush(self):
        resp = self._requests.post(self.url, json={"events": self._buf})
        resp.raise_for_status()
        self._buf.clear()
```

So the live data path is: **camera → detection loop → HttpSink batches → `POST /events/ingest` →
DB → dashboard polls `/metrics` every 2s.** End-to-end latency is a couple of seconds. You can watch
exactly this happen with the simulator, which stands in for a live camera by generating fresh events
and POSTing them:

```bash
python tools/simulate.py --api http://localhost:8000 --live --store ST1008
```

**The architecture point that makes 40 stores feasible.** Video is huge; events are tiny (a few
hundred bytes of JSON). So in production you run the **detection on a small computer inside each
store** (an "edge" box, ideally with a cheap GPU), and send only the **events** over the network to
one central API. The central API never touches video. That's the entire reason the system is split
into `pipeline/` (heavy, per-store, near the cameras) and `app/` (light, central): the contract
between them is the small event JSON, so you can scale the expensive half horizontally — one box per
store — without touching the analytics half.

**The honest constraints of real time:**
- **Throughput.** YOLOv8-nano on a CPU does ~5–15 fps; a real deployment uses a GPU or processes at
  a reduced frame rate (`vid_stride`) — you rarely need 30 fps to count shoppers; 5 fps is plenty.
- **One model per camera stream** (or batched on a GPU). 5 cameras × 40 stores = 200 streams → that's
  200 edge inferences, which is exactly why you push detection to the edge rather than centralising
  video.
- **State lives in memory** (the gallery, dwell states). For a 24/7 feed you'd periodically prune the
  gallery (the 15-min TTL already does most of this) so memory stays bounded.

---

*If any single section is still fuzzy, tell me which one and I'll expand it with more pictures and
smaller steps. Nothing here is too small to ask about.*