# SCHEMA.md — the event schema (final dataset)

This is the authoritative reference for the event format the system speaks, based on the
dataset's `sample_events.jsonl`. It supersedes the simplified single-schema described in the
original problem-statement PDF.

## The shape: three event families (the "wire" schema)

The real data is **three different event shapes** from three subsystems, with inconsistent key
names. Our pipeline **emits these exact shapes** and **adds one `visitor_id`** to every event so
a person can be stitched across families (entry uses `id_token`, zone/queue use `track_id`; all
three also carry `visitor_id` with the same stable Re-ID value).

**1. entry / exit / reentry** (the door subsystem)
```jsonc
{
  "event_type": "entry",            // "entry" | "exit" | "reentry"
  "id_token": "VIS_000001",          // native person key for this family
  "visitor_id": "VIS_000001",        // ADDED: stable cross-family link (== id_token here)
  "store_code": "ST1008", "store_id": "ST1008",
  "camera_id": "CAM_ENTRY_01",
  "event_timestamp": "2026-04-10T14:40:00.834168",
  "is_staff": false,
  "gender_pred": null, "age_pred": null, "age_bucket": null,
  "is_face_hidden": true,            // footage is face-blurred
  "group_id": null, "group_size": null,
  "confidence": 0.9
}
```

**2. zone_entered / zone_exited** (the floor/zone subsystem)
```jsonc
{
  "event_type": "zone_entered",      // "zone_entered" | "zone_exited"
  "track_id": "VIS_000005",          // native person key for this family
  "visitor_id": "VIS_000005",        // ADDED link
  "store_id": "ST1008", "camera_id": "CAM_SKIN_01",
  "zone_id": "SKIN", "zone_name": "SKIN", "zone_type": "SHELF", "is_revenue_zone": "Yes",
  "event_time": "2026-04-10T14:40:29.0",
  "zone_hotspot_x": 412.6, "zone_hotspot_y": 238.4,
  "gender": null, "age": null, "age_bucket": null,
  "confidence": 0.8
}
```

**3. queue_completed / queue_abandoned** (the billing subsystem — one event per billing episode)
```jsonc
{
  "queue_event_id": "83465331-08b9-48d3-99f7-3612bcab2463",
  "event_type": "queue_completed",   // "queue_completed" | "queue_abandoned"
  "track_id": "VIS_000007", "visitor_id": "VIS_000007",
  "store_id": "ST1008", "camera_id": "CAM_BILLING_01",
  "zone_id": "BILLING", "zone_name": "Billing Counter Queue", "zone_type": "BILLING", "is_revenue_zone": "Yes",
  "queue_join_ts": "...", "queue_served_ts": "...", "queue_exit_ts": "...",
  "wait_seconds": 8, "queue_position_at_join": 2, "abandoned": false,
  "confidence": 0.9
}
```

Note the inconsistencies that are real and intentional: `store_code` vs `store_id`, `id_token` vs
`track_id`, `event_timestamp` vs `event_time` vs `queue_join_ts`, lowercase event types.

## The boundary normalizer (`app/schema_compat.py`)

Rather than spread that messiness through the codebase, **one function, `normalize_event(raw)`,
maps any of these shapes (plus our own output and the older canonical shape) into one clean
internal record** that `app.models.Event` validates and `app.db.EventRow` stores. Rules:

| concern | rule |
|---|---|
| person key | `visitor_id` > `id_token` > `track_id` (first present wins) |
| store key | `store_id` or `store_code`; `store_<digits>` → `ST<digits>` (e.g. `store_1076`→`ST1076`); other ids untouched |
| timestamp | first of `event_timestamp` / `event_time` / `timestamp` / `queue_join_ts` |
| event_type | lowercase family names → canonical UPPER (`queue_completed`→`BILLING_QUEUE_JOIN`, `queue_abandoned`→`BILLING_QUEUE_ABANDON`, etc.) |
| event_id | explicit `event_id` passed through (strictly validated as UUID); else a valid `queue_event_id`; else a **deterministic uuid5** of the content (so re-ingest is idempotent) |
| rich fields | gender/age/group, zone_name/type, hotspot, wait_seconds, abandoned, etc. preserved under `metadata` |

The internal canonical record (what the DB/analytics use) is unchanged from before — that's why
the analytics, metrics, and tests didn't need to relearn the world. The wire schema lives only at
the edges (pipeline output + ingest input).

## Conversion on the new schema

The billing subsystem now gives a **direct outcome** per visitor: `queue_completed` (with
`abandoned=false`) means they were *served*. So conversion uses the best available signal:

> a visitor is **converted** if they were *served at billing* (a `queue_completed`, not abandoned)
> **OR** they were in the billing zone within 5 minutes before a POS transaction.

This makes conversion meaningful even for a store with no POS feed, and gives a real, non-zero
number for ST1008 (≈22%) from the billing-camera queue events — where the POS-only definition read
0% because no sale fell inside the 2.5-minute clip window. Dwell is computed from
`zone_entered`→`zone_exited` timestamp pairs (the wire zone events carry no `dwell_ms`).

## Two stores

| store_id | which | cameras (role) | POS? |
|---|---|---|---|
| `ST1008` | Brigade Road, Bangalore (Store 1) | CAM 3=entry, CAM 1 & 2=floor (CAM 2 split MAKEUP/HAIRCARE), CAM 5=billing | yes (24 orders, 2026-04-10) |
| `ST2002` | Store 2 (placeholder id) | 2× entry, 1× floor (DISPLAY), 1× billing | no (cameras not time-synced; aligned to one window) |

Layouts are in `data/store_layout.json` (zones, door lines, per-camera `video`/`fps`/`start`),
derived from the provided floor-plan PNGs (`Store 1 - layout.png`, `store 2 - layout.png`).

## Staff exclusion & queue depth (detection)

Uniform colours differ between stores and are declared in `store_layout.json` under `staff_uniform`
(a list). `StaffClassifier` runs the appropriate HSV pixel-fraction check against the **torso
region** of each person crop (rows 25–70%, cols 20–80% of the bounding box):

| Store | Uniform | Detector | HSV rule |
|---|---|---|---|
| ST1008 (Brigade Road) | Red | `red_uniform_fraction` | hue 0–10 or 170–180, S ≥ 90, V ≥ 60 |
| ST2002 | Black/dark | `dark_uniform_fraction` | V < 60 **and** S < 60 |

If any configured check exceeds ~20% of the torso crop the person is flagged `is_staff=true` and
excluded from unique visitors, zone visits, and the **billing queue** (cashiers standing at the
counter inflate queue depth if not filtered out).

**Why the torso crop matters for black uniforms:** dark pixels are common in retail scenes —
customer hair (top of every bounding box), dark jeans (bottom), shadows, shelf edges, dark
handbags. Without restricting to the torso strip, `dark_uniform_fraction` would produce
unacceptable false positives on customers in dark clothing. The torso crop eliminates those
sources and leaves only the shirt/jacket region. **Queue depth** is the number of non-staff
customers actually present at join time (seen within the last 5 s), not a running total of
everyone who ever stepped up — which is what caused an earlier build to report a false spike of 5
when only ~2 customers were checking out.

## Where to look in code
- emit the wire shapes: `pipeline/emit.py` (`make_entry`, `make_zone`, `make_queue`)
- normalize on ingest: `app/schema_compat.py` (`normalize_event`)
- canonical record + validation: `app/models.py`
- sessions + conversion: `app/analytics.py` (`build_window`)
- tests: `tests/test_schema_compat.py`
