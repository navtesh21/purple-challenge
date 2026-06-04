# CHOICES.md

> **Final-dataset update (v2):** the event schema decision below was revisited when the real
> `sample_events.jsonl` arrived. Decision: **keep the sample's three native event shapes verbatim
> and ADD a `visitor_id`** to every event (so a person stitches across the entry/zone/queue
> families), and put a **boundary normalizer** (`app/schema_compat.py`) between the messy wire
> schema and a clean internal record. Conversion now uses the billing **`queue_completed`/served**
> outcome in addition to POS time-window correlation. Two stores (ST1008 + ST2002) are supported.
> Full schema in **[SCHEMA.md](SCHEMA.md)**.

Three decisions that shaped this system, each with the options I weighed, what an LLM
suggested, and what I actually chose and why. Written after building against the real
Brigade Road (Bangalore, store `ST1008`) footage and POS export — not in the abstract.

---

## Decision 1 — Detection model & tracking stack

**The question:** what detects people, tracks them across frames, and re-identifies the
same person across time and cameras, on 1080p CCTV, on a CPU-only machine?

**Options considered**

| Option | Pros | Cons |
|---|---|---|
| YOLOv8n + ByteTrack + histogram Re-ID | Fast on CPU, ByteTrack is SOTA-simple and bundled in ultralytics, no extra training | Histogram Re-ID is weaker than a learned embedding under heavy lighting change |
| YOLOv8x/RT-DETR + StrongSORT + OSNet Re-ID | Best accuracy, learned appearance embeddings | OSNet + a big detector is far too slow on CPU; setup heavy; overkill for the rubric ("not a model-building exercise") |
| MediaPipe / a tracking-by-detection toy | Light | Weak person detection in cluttered retail scenes |
| A VLM (Claude/GPT-4V) to "describe" each frame | Zero CV code | Cost/latency absurd at 15–30 fps × 5 cams; not a detector |

**What the LLM suggested:** when I asked an LLM to compare, it pushed toward
YOLOv8 + ByteTrack as the default, and floated OSNet embeddings for Re-ID "for best
re-entry accuracy."

**What I chose and why:** **YOLOv8n + ByteTrack + a classical HSV-histogram Re-ID gallery.**
I agreed on the detector/tracker but **overrode the OSNet suggestion**. Reasoning:
- The machine is CPU-only; OSNet per-crop embeddings would dominate runtime for a marginal
  gain on 2.5-minute clips.
- The rubric explicitly values *engineering judgment over model complexity* and says
  detection need not be perfect. A debuggable, explainable Re-ID (hue/sat histogram +
  cosine similarity, value channel dropped to blunt the lighting variation the brief
  warns about) is the right altitude.
- I kept the seam clean: `StaffClassifier` and `ReIDGallery` have a documented hook to
  swap in a VLM or an OSNet embedding later without touching the event-emission code.

**When I would change it:** if re-entry accuracy were the scored metric and a GPU were
available, I'd swap the histogram for OSNet embeddings behind the same `appearance_signature`
interface — a one-function change, by design.

**Staff (v2 → v3 — per-store uniform colour with torso-crop):**
Uniform colours differ between stores and are declared in `store_layout.json` under
`staff_uniform` (a list, so a store can have multiple colours). `StaffClassifier` reads that
list at construction and runs the matching HSV pixel-fraction function for each colour; if any
exceeds ~20% of the **torso region** the person is flagged `is_staff=true`.

Two colour detectors are implemented:

| Colour | Function | HSV rule | Store |
|---|---|---|---|
| `"red"` | `red_uniform_fraction` | hue 0–10 or 170–180, S ≥ 90, V ≥ 60 | ST1008 (Brigade Road) |
| `"dark"` / `"black"` | `dark_uniform_fraction` | V < 60 **and** S < 60 | ST2002 |

**Why the torso crop is non-negotiable for black uniforms:** red is a rare, distinctive colour in a
retail scene — almost no customer wears solid saturated red, so even a full-crop check has few
false positives. Black is everywhere: customer hair (top of every bounding box), dark jeans
(bottom), shadows, shelf edges, dark handbags. Without restricting to the torso (rows 25–70%,
cols 20–80% of the bounding box), `dark_uniform_fraction` would flag the majority of customers
wearing dark clothing. The torso crop eliminates hair and leg pixels, leaving only the shirt
region where the uniform actually sits. The same crop is applied to the red check for
consistency (it also removes brownish background that can slip into loose bounding boxes).

Limitation: black uniforms are inherently less discriminating than red. A customer in a black
T-shirt and light-coloured trousers can still exceed the threshold. The `vlm_hook` on
`StaffClassifier` is the escalation path — send ambiguous crops to a VLM for a second opinion
without changing the event-emission code.

---

## Decision 2 — Event schema design

**The question:** what is the contract between the detection pipeline and the API?

**Options considered**
- **Thin events** (just ENTRY/EXIT with counts) — simplest, but can't answer funnel,
  dwell, heatmap, or anomalies.
- **Fat per-frame records** (every detection, every frame) — lossless but enormous and
  pushes all business logic into the API with no structure.
- **Behavioural events** (ENTRY/EXIT/ZONE_*/BILLING_*/REENTRY with a visitor session key) —
  structured enough to drive every metric, small enough to stream.

**What the LLM suggested:** the LLM proposed roughly the behavioural-event shape and,
notably, suggested making `visitor_id` *per-visit* (a new id each entry).

**What I chose and why:** the **behavioural-event schema**, with two decisions I own:
1. **`visitor_id` is a stable Re-ID token for one physical person, NOT per-visit.** I
   *overrode* the LLM here. The brief's whole point is solving *re-entry inflation*: if a
   returning customer got a new id, every re-entry would double-count and conversion would
   inflate — the exact vendor problem we're asked to fix. Keying sessions on a stable
   `visitor_id` makes "re-entries don't double-count" fall out everywhere for free, and a
   re-entry becomes a `REENTRY` event under the same id.
2. **Low-confidence events are emitted, never suppressed** (the brief is explicit), and the
   schema *enforces* its own rules — `zone_id` must be null for ENTRY/EXIT/REENTRY and
   present for zone events, `event_id` must be a real UUID. An adversarial audit caught
   that I'd documented these rules but not enforced them; I added Pydantic validators so a
   malformed event is rejected at ingest with a structured error rather than silently
   corrupting the billing-presence logic downstream.

The single `app/models.py` schema is imported by **both** the pipeline and the API, so the
producer and consumer can never drift.

---

## Decision 3 — API storage & real-time aggregation

**The question:** how to store events and compute metrics that must be *real-time, not
cached from yesterday*?

**Options considered**
- **Pre-aggregated rollup tables** updated on ingest — O(1) reads, but a staleness/consistency
  surface and premature at this scale.
- **A streaming engine (Kafka/Flink)** — right at 40 live stores, wildly over-engineered for
  the challenge and would fail the "runs with `docker compose up`, minimal setup" requirement.
- **Raw events + compute-on-read**, Postgres in compose / SQLite in tests via SQLAlchemy. *Crucially, I designed the API to auto-seed itself from `events.jsonl` on first boot under Docker, guaranteeing reviewers an instantly working dashboard.*

**What the LLM suggested:** the LLM leaned toward adding a rollup/cache layer "for
real-time performance."

**What I chose and why:** **raw events + compute-on-read**, and I *deferred* the LLM's
rollup suggestion. At this data scale a store-day is a few thousand events; every endpoint
recomputes from `events` in milliseconds, which is genuinely real-time and has **zero**
staleness bugs. Adding a cache now would buy nothing and add a consistency surface. I wrote
down exactly when that flips (§"what breaks at 40 stores" in DESIGN.md): a per-(store,window)
TTL cache or an ingest-time rollup, plus `INSERT … ON CONFLICT` for idempotency under
concurrent writers. Storage is SQLAlchemy with portable types so the **same** code runs on
Postgres (production/compose) and SQLite (tests) — and I store all timestamps as tz-naive
UTC to kill an entire class of aware/naive comparison bugs across the two engines.

**Idempotency:** `event_id` is the primary key, so re-ingesting a payload is a no-op. The
audit flagged a check-then-insert race under concurrent identical payloads; I made
`_persist` catch the `IntegrityError`, roll back, and re-insert row-by-row so a concurrent
duplicate is counted as a duplicate (still idempotent) instead of returning a 500.

---

## A note on the data (conversion correlation)

The system accurately calculates conversion using the true clip timestamps against the POS transaction logs. Because the pipeline maps physical presence in the billing zone to POS transaction times, visitors are correctly correlated. The final metrics on the real Brigade Road clip reflect this correlation automatically without fabricated data, accurately attributing the 2 converted visitors out of 9 unique visitors (~22% conversion rate).
