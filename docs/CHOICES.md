# CHOICES.md

Three decisions where there was a real fork in the road. For each: what I
considered, what the AI suggested, and what I actually shipped.

## 1. Detection model

**Options considered.** YOLOv8/YOLO11 (nano through medium), RT-DETR, and
MediaPipe. For tracking, ByteTrack vs BoT-SORT. For re-identification, a full
OSNet/torchreid model vs a cheap appearance descriptor.

**What the AI suggested.** It pointed at YOLOv8 + ByteTrack as the common,
well-documented starting point, and suggested OSNet for re-entry.

**What I chose and why.** YOLO11n with BoT-SORT, and a colour-histogram
descriptor instead of OSNet. Three reasons. First, the clips are short
(~85–125s) portrait 960×1080 at 25fps, and the people are large and reasonably
separated — a nano detector is plenty, and it keeps the whole clip processing in
seconds on a T4. Second, I switched ByteTrack → BoT-SORT after seeing id
switches at the door; BoT-SORT's appearance term holds ids together better
through the brief occlusion of the door frame, and re-entry detection leans on
stable ids. Third, I dropped OSNet on purpose: with un-synced cameras there's no
cross-camera linking to do, so a heavyweight Re-ID model would add a torch
dependency and minutes of runtime to solve a problem this footage can't pose.
The histogram match is enough to catch the within-camera re-entry case (someone
steps out and comes back) which is what the brief's REENTRY event actually asks
for. If the production footage were synced, OSNet is exactly where I'd reinvest.

## 2. Event schema

**Options considered.** A single flat event row for every event type, versus
typed per-event tables, versus a thin core with a free-form metadata blob.

**What the AI suggested.** Closely mirror the schema in the brief and store each
event as one row.

**What I chose and why.** I kept the brief's schema as the contract, stored each
event as one flat row, and lifted only the fields I filter or aggregate on
(`store_id`, `visitor_id`, `event_type`, `ts`, `zone_id`, `is_staff`,
`queue_depth`) into indexed columns while keeping the full original payload in a
JSON column. This means the metric queries are plain indexed SQL, but I never
lose a vendor's extra fields. The one place I went beyond "just mirror it" was
the ingest layer: real CCTV vendors are inconsistent, and the sample events I
was given already used a different dialect than the schema box (lowercase types,
`store_code`, `id_token`, `event_time`). So `EventIn` normalises known aliases
onto the canonical schema before validation. The trade-off is that ingest is
permissive — but it's permissive in a bounded, documented way (a fixed alias map
and an event-type whitelist), and anything it can't resolve is rejected per-event
with a reason, never silently coerced.

## 3. API architecture: compute-on-read vs materialised metrics

**Options considered.** Pre-aggregate metrics into rollup tables on ingest (fast
reads, stale-risk, more moving parts), versus compute every metric on read
straight from the events table.

**What the AI suggested.** For "real-time at 40 stores" it leaned toward a
materialised/rollup approach with a cache.

**What I chose and why.** Compute-on-read, and I'd defend that for the stage this
system is at. The brief is explicit that metrics must be real-time and not cached
from yesterday; compute-on-read makes staleness structurally impossible and keeps
the code small enough to actually reason about and test. The event volumes here
are tiny, and even at realistic store volumes a day's events per store are well
within an indexed query's comfort zone. I know where this breaks — that's
literally one of the example follow-up questions — and the answer is the
honest one: the first thing to fall over at 40 live stores is the per-request
`converted_visitors` scan crossing POS and billing events on every `/metrics` and
`/anomalies` call. The fix when that day comes is a rollup table refreshed on
ingest (or a materialised view), keyed by store and time-bucket, with the
on-read path kept as the source of truth. I chose not to build that now because
it would be optimising for a load this submission doesn't have, at the cost of
clarity it does need.

## A note on the VLM

I considered a VLM (Claude/GPT-4V) for staff detection — prompting it per track
crop with something like *"is this person wearing a pink/magenta store uniform?
answer yes/no."* I tested the idea and chose the rule-based HSV pink test
instead: the uniform colour is distinctive enough that a histogram threshold gets
it, and a per-crop VLM call would add latency and cost to every frame for a
decision a cheap colour rule already makes well. Where a VLM would earn its keep
is zone classification in a store I don't have a layout for — describing the
shelf contents from a frame — and that's the first place I'd reach for one next.
