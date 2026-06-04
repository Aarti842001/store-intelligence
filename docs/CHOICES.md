# Choices

Three real forks in the road. For each: the options, what the AI suggested, what
I actually shipped, and why.

## 1. Detection model

- **Options:** YOLOv8 / YOLO11 (nano–medium), RT-DETR, MediaPipe · tracking: ByteTrack vs BoT-SORT · re-ID: full OSNet/torchreid vs a cheap appearance descriptor.
- **AI suggested:** YOLOv8 + ByteTrack as the well-documented default, OSNet for re-entry.
- **I shipped:** YOLO11n + BoT-SORT + a colour-histogram descriptor (no OSNet).
- **Why:**
  - The clips are short (~85–125s), portrait 960×1080 at 25fps, and the people are large and well-separated — a nano detector is plenty and finishes a clip in seconds on a T4.
  - I switched ByteTrack → BoT-SORT after seeing id switches at the door. BoT-SORT's appearance term holds ids together through the brief door-frame occlusion, and re-entry detection leans on stable ids.
  - I dropped OSNet on purpose: with un-synced cameras there's no cross-camera linking to do, so a heavyweight Re-ID model would add a torch dependency and minutes of runtime to solve a problem this footage can't pose. The histogram match is enough for the within-camera re-entry the `REENTRY` event actually asks for.
  - If the production footage were synced, OSNet is exactly where I'd reinvest.

## 2. Event schema

- **Options:** one flat row per event · typed per-event tables · a thin core + free-form metadata blob.
- **AI suggested:** mirror the brief's schema, store each event as one row.
- **I shipped:** the brief's schema as the contract, one flat row per event, with the fields I filter/aggregate on (`store_id`, `visitor_id`, `event_type`, `ts`, `zone_id`, `is_staff`, `queue_depth`) lifted into indexed columns and the full original payload kept in a JSON column.
- **Why:**
  - Metric queries stay plain indexed SQL, but I never lose a vendor's extra fields.
  - The one place I went past "just mirror it" was the ingest layer. The sample events I was given already used a different dialect than the schema box — lowercase types, `store_code`, `id_token`, `event_time`. So `EventIn` normalises known aliases onto the canonical schema before validation.
  - Trade-off: ingest is permissive — but in a bounded, documented way (a fixed alias map and an event-type whitelist). Anything it can't resolve is rejected per-event with a reason, never silently coerced.

## 3. API architecture: compute-on-read vs materialised metrics

- **Options:** pre-aggregate metrics into rollup tables on ingest (fast reads, stale-risk, more moving parts) vs compute every metric on read straight from the events table.
- **AI suggested:** for "real-time at 40 stores," a materialised/rollup approach with a cache.
- **I shipped:** compute-on-read.
- **Why:**
  - The brief is explicit that metrics must be real-time and not cached from yesterday. Compute-on-read makes staleness structurally impossible and keeps the code small enough to actually reason about and test.
  - Event volumes here are tiny, and even at realistic per-store/day volumes an indexed query is well within its comfort zone.
  - I know where it breaks — it's literally one of the example follow-up questions. At 40 live stores the first thing to fall over is the per-request `converted_visitors` scan crossing POS and billing events on every `/metrics` and `/anomalies` call. The fix when that day comes is a rollup table (or materialised view) refreshed on ingest, keyed by store + time-bucket, with the on-read path kept as the source of truth.
  - I chose not to build that now because it optimises for a load this submission doesn't have, at the cost of clarity it does need.

## A note on the VLM (considered, didn't ship)

- **Idea:** prompt a VLM (Claude / GPT-4V) per track crop — *"is this person wearing a pink/magenta store uniform? answer yes/no."*
- **What I shipped:** the rule-based HSV pink test.
- **Why:** the uniform colour is distinctive enough that a histogram threshold gets it, and a per-crop VLM call would add latency and cost to every frame for a decision a cheap colour rule already makes well.
- **Where a VLM would earn its keep:** zone classification in a store I don't have a layout for — describing the shelf contents from a frame. That's the first place I'd reach for one next.
