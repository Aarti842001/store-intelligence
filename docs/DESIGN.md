# Design

Raw store CCTV in, a live conversion number out. Four stages, kept deliberately
decoupled because they have very different runtime needs.

## What I built

- Detection watches three camera feeds and emits behavioural events (entry/exit, zone visits, billing queue).
- An API ingests those events, correlates them with POS receipts, and serves the metrics.
- One question drives all of it: *of the people who walked in, how many bought, and where did we lose the rest?*

## The pipeline

**1. Detection — `pipeline/detect_store.py`** (the only GPU-bound part; I run it on a Colab T4)

- YOLO11n for person detection, BoT-SORT for tracking.
- One handler per camera, because the cameras do different jobs:
  - Entry cam → horizontal tripwire (dead-band + cooldown) → `ENTRY` / `EXIT`
  - Floor cam → track centroid vs zone polygons → `ZONE_ENTER` / `ZONE_EXIT` / `ZONE_DWELL`
  - Billing cam → queue region + debounce → `BILLING_QUEUE_JOIN` / `ABANDON`
- Staff flagged by their pink uniform (an HSV colour test) and excluded from customer metrics.
- Re-entry caught with a colour-histogram match against recently-exited tracks → `REENTRY`, not a second `ENTRY`.
- Zone names/polygons read from `store_layout.json` when supplied; calibrated Store-2 fallback otherwise.
- Output: one JSON event per line in `events.jsonl`.

**2. Event stream — `scripts/replay.py`**

- Detection writes a file; the API reads events over HTTP. The file in between is the decoupling point.
- Lets me run detection on a beefy machine and replay later, and it drives the Part E live demo (`--realtime` paces events by their own timestamps, so the dashboard updates as if a real store were trickling them in).

**3. API — `app/` (FastAPI + PostgreSQL)**

- Every metric is computed on read from the events table — nothing pre-aggregated — so a freshly ingested event moves the numbers immediately. That's what "real-time, not cached from yesterday" means.
- Ingest is idempotent: `event_id` is the primary key, so replaying a batch is a no-op.
- POS correlation is store + 5-minute time window, exactly as the brief specifies.

**4. Consumers**

- A small web dashboard polls `/metrics`, `/funnel`, `/anomalies` and `/health` every 2 seconds.
- An on-call engineer would live on `/health` and `/anomalies`.

## The honest limitation (the thing I'd flag first)

- The clips aren't time-synced — the wall-clocks burned into the frames are days apart between cameras.
- Each camera's tracker mints its own visitor ids, so I genuinely cannot link "entered on CAM1" to "paid on CAM6".
- So I didn't fake it. The funnel counts distinct visitors **per stage**, and conversion is a store/window ratio: billing-zone visitors with a POS receipt within five minutes, over unique entrants.
- The metric code is written so that **if** visitor ids were consistent across cameras — which is what the grader's clean event set looks like — the stages chain per-visitor with no code change.
- Production fix: cross-camera Re-ID with OSNet appearance embeddings. I left it out on purpose — this footage can't support it, and I'd rather ship something truthful than something that looks clever and lies.

## Storage & failure behaviour

- Postgres over SQLite on purpose — so I could actually demonstrate the production behaviours the brief grades: idempotent upserts via `ON CONFLICT`, and a clean HTTP 503 with a structured body (no stack trace) when the DB is unreachable.
- Falls back to SQLite with zero config for local runs and the test suite, so `pytest` needs no infrastructure.

## AI-Assisted Decisions

**1. Tripwire line — I overrode the AI.**

- It suggested the textbook default: counting line at the vertical middle of the frame (~0.45 of height).
- Wrong on this footage — there's a frosted-glass vestibule, so the real tile-to-wood threshold sits lower, around 0.57.
- I moved the line; entry accuracy went from badly under-counting to roughly 10 of 11 true entries.
- Lesson I'd repeat: calibrate against actual frames, never a default.

**2. Conversion semantics — I pushed back and reframed.**

- The AI's first instinct was to link each billing visitor to their entry session for a clean per-person funnel.
- I'd already found the cameras aren't synced, so that link is fiction here.
- We settled on the population-level / store-window interpretation, and I documented it as a known limitation rather than pretending the ids line up.
- This is the decision I most want to be asked about in the follow-up.

**3. Tests — I agreed, then hardened.**

- I had the assistant draft the pytest cases from a plain-language spec.
- Then I changed two things: made all test timestamps relative to "now" (the metrics window is rolling, so hard-coded dates silently fell outside it and gave false passes), and added the alias-normalisation and DB-down cases it had skipped.
- The prompt and the exact edits are recorded at the top of each test file.
