"""
Store Intelligence - detection layer (v2, calibrated after the first run).

The v1 run over-counted badly: 136 billing events, more exits than entries,
half the zone-enters never closed, zero dwell. All of it traced to one thing
-- ByteTrack hands out a fresh track id every time someone is briefly
occluded (constant at a crowded counter), so every reappearance looked like a
new person. v2 fixes identity stability and debounces every state change in
*seconds*, not frames:

  * tracker swapped to BoT-SORT (keeps ids through short occlusions)
  * entry tripwire has a dead-band + per-track cooldown (no loiter flip-flop)
  * billing join/abandon must persist for a real duration to count
  * tracks seen <3 frames are ignored; a track that vanishes mid-zone still
    gets its ZONE_EXIT flushed so enters and exits balance
  * re-entry match is stricter and each exit can only be matched once

Footage facts (unchanged): clips are 960x1080, 25fps, ~2min; camera id and a
wall-clock are burned into the frame; the clocks are days apart so the cameras
are NOT synchronised -> each camera owns its own visitor sessions.

Run (in Colab, code already in the kernel):
    run("/content/Store 2", "events.jsonl")

Ritu / Store-Intelligence challenge.
"""

import argparse
import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from ultralytics import YOLO


# --------------------------------------------------------------------------
# config -- store-specific knobs only. Pipeline logic stays generic.
# --------------------------------------------------------------------------

STORE_ID = "STORE_MUM_002"
MODEL = "yolo11n.pt"
TRACKER = "botsort.yaml"        # better id persistence than bytetrack on CCTV
DET_CONF = 0.25                 # keep low-conf dets, we DON'T silently drop them
FRAME_STRIDE = 2                # process every 2nd frame (~12.5 fps)

MIN_TRACK_FRAMES = 3            # ignore tracks seen fewer than this many proc frames
STALE_S = 1.5                  # a track unseen this long is treated as gone
DWELL_INTERVAL_S = 30          # emit ZONE_DWELL every 30s of continuous dwell
GROUP_WINDOW_S = 4.0           # entries within this window are tagged one group

# entry tripwire (fraction of frame height). CAM1: mall at top, store floor at
# bottom near lens, so crossing the line downward == walking in.
ENTRY_LINE_FRAC = 0.57         # the tile->wood threshold (the door floor-track),
                               # NOT the frosted-glass vestibule above it
ENTRY_BAND_FRAC = 0.03         # dead band +/- around the line; ignore foot in it
ENTRY_COOLDOWN_S = 3.0         # one counted crossing per track per this long

# re-entry: HSV colour hist is weak, so use a time-decayed bar -- a returning
# customer almost always comes back within seconds, so recent exits match at a
# looser threshold while old ones stay strict. Each exit is matched once.
REENTRY_WINDOW_S = 120
REENTRY_RECENT_S = 30          # exit younger than this == a "quick" re-entry
REENTRY_SIM_RECENT = 0.50      # looser bar for quick re-entries
REENTRY_SIM = 0.72             # strict bar for older matches

# billing queue debounce (the v1 churn killer)
QUEUE_MIN_PRESENCE_S = 1.2     # must linger this long before it's a JOIN
QUEUE_MIN_ABSENCE_S = 2.0      # must be gone this long before it's a leave
QUEUE_MIN_DWELL_S = 3.0        # ignore pass-throughs; abandon needs real queueing

STAFF_PINK_RATIO = 0.22        # >22% pink pixels in the torso -> staff

# These are the calibrated fallback geometries for Store 2's floor and billing
# cameras (normalised 0-1 coords). They're only used when no store_layout.json
# is supplied -- see load_layout() below.
ZONES_CAM2 = {
    "LEFT_SHELF":   [(0.00, 0.28), (0.40, 0.28), (0.40, 1.00), (0.00, 1.00)],
    "RIGHT_SHELF":  [(0.62, 0.28), (1.00, 0.28), (1.00, 1.00), (0.62, 1.00)],
    "CENTER_AISLE": [(0.30, 0.55), (0.70, 0.55), (0.70, 1.00), (0.30, 1.00)],
}
QUEUE_REGION = [(0.26, 0.18), (0.76, 0.18), (0.76, 0.58), (0.26, 0.58)]


def load_layout(path, floor_cam="CAM2", billing_cam="CAM6"):
    """Read zone names + polygons from a store_layout.json when one is given.

    The brief says zone names come from store_layout.json, so the file is the
    source of truth when present; we only fall back to the calibrated defaults
    above when it's missing or unreadable. That keeps the pipeline runnable on
    Store 2 out of the box without hardcoding it to one store.

    Expected shape (coords normalised 0-1, same convention as the defaults):

        {"cameras": {"CAM2": {"zones": {"SKINCARE": [[x, y], ...], ...}},
                     "CAM6": {"queue_region": [[x, y], ...]}}}

    A flat {"zones": {...}, "queue_region": [...]} is also accepted. Returns
    (floor_zones, queue_region); either is None to signal "use the default".
    """
    if not path or not os.path.exists(path):
        return None, None
    try:
        data = json.load(open(path))
    except Exception as e:  # noqa: BLE001 -- any parse failure -> fall back
        print(f"  layout: could not read {path} ({e}); using built-in zones")
        return None, None

    cams = data.get("cameras", {})
    zones_raw = cams.get(floor_cam, {}).get("zones") or data.get("zones")
    queue_raw = cams.get(billing_cam, {}).get("queue_region") or data.get("queue_region")

    def _poly(pts):
        return [(float(x), float(y)) for x, y in pts]

    floor_zones = None
    if zones_raw:
        try:
            floor_zones = {str(n): _poly(p) for n, p in zones_raw.items()}
            print(f"  layout: loaded {len(floor_zones)} floor zones from {path}")
        except Exception as e:  # noqa: BLE001
            print(f"  layout: bad zone geometry ({e}); using built-in zones")

    queue_region = None
    if queue_raw:
        try:
            queue_region = _poly(queue_raw)
            print(f"  layout: loaded queue region from {path}")
        except Exception as e:  # noqa: BLE001
            print(f"  layout: bad queue geometry ({e}); using built-in region")

    return floor_zones, queue_region

CLIPS = [
    ("entry 1.mp4",     "CAM1", "entry",   "2026-03-29T19:39:06Z"),
    ("entry 2.mp4",     "CAM1", "entry",   "2026-03-08T13:39:00Z"),
    ("zone.mp4",        "CAM2", "floor",   "2026-03-08T15:27:51Z"),
    ("billing_area.mp4","CAM6", "billing", "2026-03-08T18:27:51Z"),
]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def poly_px(poly_frac, w, h):
    return np.array([(int(x * w), int(y * h)) for x, y in poly_frac], np.int32)


def in_poly(point, poly):
    return cv2.pointPolygonTest(poly, point, False) >= 0


def torso_crop(frame, box):
    x1, y1, x2, y2 = [int(v) for v in box]
    bh = y2 - y1
    ty1, ty2 = y1 + int(0.15 * bh), y1 + int(0.55 * bh)
    crop = frame[max(0, ty1):ty2, max(0, x1):x2]
    return crop if crop.size else None


def is_pink_uniform(crop):
    if crop is None or crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (150, 70, 60), (179, 255, 255))
    m2 = cv2.inRange(hsv, (0, 70, 60), (8, 255, 255))
    pink = cv2.countNonZero(m1 | m2)
    return (pink / (crop.shape[0] * crop.shape[1])) > STAFF_PINK_RATIO


def appearance_hist(crop):
    if crop is None or crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist


def hist_sim(a, b):
    if a is None or b is None:
        return 0.0
    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


def iso(start_dt, frame_idx, fps):
    return (start_dt + timedelta(seconds=frame_idx / fps)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_visitor():
    return "VIS_" + uuid.uuid4().hex[:6]


def open_video(path):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return cap, fps, w, h


def track_frame(model, frame):
    """detector + tracker on one frame. yields (box, track_id, conf)."""
    res = model.track(frame, persist=True, classes=[0], conf=DET_CONF,
                     tracker=TRACKER, verbose=False)[0]
    if res.boxes.id is None:
        return []
    return list(zip(res.boxes.xyxy.cpu().numpy(),
                    res.boxes.id.int().cpu().tolist(),
                    res.boxes.conf.cpu().tolist()))


# --------------------------------------------------------------------------
# event sink (canonical schema lives here, in one place)
# --------------------------------------------------------------------------

class EventSink:
    def __init__(self, path):
        self._fh = open(path, "w")
        self.count = 0

    def emit(self, camera_id, visitor_id, event_type, ts, *,
             zone_id=None, dwell_ms=0, is_staff=False, confidence=1.0,
             queue_depth=None, sku_zone=None, session_seq=0):
        ev = {
            "event_id": str(uuid.uuid4()),
            "store_id": STORE_ID,
            "camera_id": camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": ts,
            "zone_id": zone_id,
            "dwell_ms": int(dwell_ms),
            "is_staff": bool(is_staff),
            "confidence": round(float(confidence), 3),
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone": sku_zone,
                "session_seq": session_seq,
            },
        }
        self._fh.write(json.dumps(ev) + "\n")
        self.count += 1

    def close(self):
        self._fh.close()


@dataclass
class FloorSession:
    visitor_id: str
    seq: int = 0
    is_staff: bool = False
    last_zone: str = None
    zone_enter_frame: int = 0
    last_dwell_frame: int = 0
    last_seen_frame: int = 0
    closed: bool = False

    def next_seq(self):
        self.seq += 1
        return self.seq


# --------------------------------------------------------------------------
# CAM1 -- ENTRY / EXIT / REENTRY via a tripwire with hysteresis
# --------------------------------------------------------------------------

def process_entry(model, video, camera_id, start_dt, sink, reid_gallery):
    cap, fps, w, h = open_video(video)
    line_y = ENTRY_LINE_FRAC * h
    band = ENTRY_BAND_FRAC * h
    cooldown = ENTRY_COOLDOWN_S * fps
    stale = STALE_S * fps

    confirmed = {}          # tid -> last *confirmed* side (outside the band)
    seen = defaultdict(int)
    visitor_of = {}         # tid -> visitor_id once it has entered
    last_cross = defaultdict(lambda: -1e9)
    last_foot = {}          # tid -> last foot_y, for exit-recovery direction
    last_seen = {}          # tid -> last frame seen
    inside = {}             # tid -> last appearance hist while it is "in" (no exit yet)
    recent_entries = []     # (frame, visitor) for group tagging
    fidx = 0

    def close_exit(tid, frame_idx, conf=0.5):
        """emit a (possibly recovered) EXIT and feed the re-id gallery."""
        vid = visitor_of.pop(tid, None)
        if vid is None:
            return
        sink.emit(camera_id, vid, "EXIT", iso(start_dt, frame_idx, fps),
                  confidence=conf, session_seq=2)
        reid_gallery.append([inside.pop(tid, None), vid, frame_idx, fps, False])

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fidx % FRAME_STRIDE:
            fidx += 1
            continue
        for box, tid, conf in track_frame(model, frame):
            seen[tid] += 1
            last_seen[tid] = fidx
            foot_y = float(box[3])
            last_foot[tid] = foot_y
            if foot_y < line_y - band:
                side = "above"
            elif foot_y > line_y + band:
                side = "below"
            else:
                continue  # inside dead band -> ambiguous, ignore
            prev = confirmed.get(tid)
            confirmed[tid] = side
            if prev is None or prev == side:
                continue
            if seen[tid] < MIN_TRACK_FRAMES:          # ghost track
                continue
            if fidx - last_cross[tid] < cooldown:      # debounce loitering
                continue
            last_cross[tid] = fidx

            crop = torso_crop(frame, box)
            staff = is_pink_uniform(crop)
            hist = appearance_hist(crop)
            ts = iso(start_dt, fidx, fps)
            if prev == "above" and side == "below":    # walked in
                vid, etype = _match_reentry(hist, reid_gallery, fidx, fps)
                if vid is None:
                    vid, etype = new_visitor(), "ENTRY"
                visitor_of[tid] = vid
                inside[tid] = hist
                gid = _group_tag(recent_entries, fidx, fps)
                recent_entries.append((fidx, vid))
                sink.emit(camera_id, vid, etype, ts, is_staff=staff,
                          confidence=conf, session_seq=1,
                          sku_zone=("GROUP:" + gid if gid else None))
            else:                                      # clean far-side exit
                close_exit(tid, fidx, conf=conf)

        # exit-recovery: a track that entered and then vanished near the top of
        # the frame (heading out the door) gets its EXIT even without a clean
        # far-side crossing. Lost at the bottom == walked into the store, not out.
        for tid in [t for t in visitor_of if (fidx - last_seen.get(t, fidx)) > stale]:
            if last_foot.get(tid, 1e9) < line_y + band:
                close_exit(tid, last_seen.get(tid, fidx))
            else:
                visitor_of.pop(tid, None)   # walked deeper in; close without exit
                inside.pop(tid, None)
        fidx += 1

    # end of clip: anyone still "inside" near the door is treated as having left
    for tid in list(visitor_of):
        if last_foot.get(tid, 1e9) < line_y + band:
            close_exit(tid, last_seen.get(tid, fidx))
    cap.release()


def _group_tag(recent_entries, fidx, fps):
    members = [v for f, v in recent_entries if (fidx - f) / fps <= GROUP_WINDOW_S]
    return ("G_" + members[0][-6:]) if members else None


def _match_reentry(hist, gallery, fidx, fps):
    best, best_sim, best_entry = None, 0.0, None
    now_s = fidx / fps
    for entry in gallery:
        g_hist, g_vid, g_f, g_fps, used = entry
        if used or (now_s - g_f / g_fps) > REENTRY_WINDOW_S:
            continue
        s = hist_sim(hist, g_hist)
        if s > best_sim:
            best, best_sim, best_entry = g_vid, s, entry
    if best_entry is None:
        return None, None
    age_s = now_s - best_entry[2] / best_entry[3]
    bar = REENTRY_SIM_RECENT if age_s <= REENTRY_RECENT_S else REENTRY_SIM
    if best_sim >= bar:
        best_entry[4] = True            # consume this exit, match it only once
        return best, "REENTRY"
    return None, None


# --------------------------------------------------------------------------
# CAM2 -- ZONE_ENTER / ZONE_EXIT / ZONE_DWELL with stale-session flushing
# --------------------------------------------------------------------------

def process_floor(model, video, camera_id, start_dt, sink, zones_norm=None):
    cap, fps, w, h = open_video(video)
    zones = {z: poly_px(p, w, h) for z, p in (zones_norm or ZONES_CAM2).items()}
    stale = STALE_S * fps
    sessions = {}
    seen = defaultdict(int)
    fidx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fidx % FRAME_STRIDE:
            fidx += 1
            continue
        ts = iso(start_dt, fidx, fps)
        active = set()
        for box, tid, conf in track_frame(model, frame):
            seen[tid] += 1
            if seen[tid] < MIN_TRACK_FRAMES:
                continue
            active.add(tid)
            foot = (int((box[0] + box[2]) / 2), int(box[3]))
            cur = next((z for z, poly in zones.items() if in_poly(foot, poly)), None)
            s = sessions.get(tid)
            if s is None:
                s = sessions[tid] = FloorSession(new_visitor())
                s.is_staff = is_pink_uniform(torso_crop(frame, box))
            s.last_seen_frame = fidx

            if cur != s.last_zone:
                if s.last_zone is not None:
                    dwell = (fidx - s.zone_enter_frame) / fps * 1000
                    sink.emit(camera_id, s.visitor_id, "ZONE_EXIT", ts,
                              zone_id=s.last_zone, dwell_ms=dwell, is_staff=s.is_staff,
                              confidence=conf, sku_zone=s.last_zone, session_seq=s.next_seq())
                if cur is not None:
                    sink.emit(camera_id, s.visitor_id, "ZONE_ENTER", ts,
                              zone_id=cur, is_staff=s.is_staff, confidence=conf,
                              sku_zone=cur, session_seq=s.next_seq())
                    s.zone_enter_frame = fidx
                    s.last_dwell_frame = fidx
                s.last_zone = cur
            elif cur is not None and (fidx - s.last_dwell_frame) / fps >= DWELL_INTERVAL_S:
                dwell = (fidx - s.zone_enter_frame) / fps * 1000
                sink.emit(camera_id, s.visitor_id, "ZONE_DWELL", ts,
                          zone_id=cur, dwell_ms=dwell, is_staff=s.is_staff,
                          confidence=conf, sku_zone=cur, session_seq=s.next_seq())
                s.last_dwell_frame = fidx

        # flush tracks that have gone stale while still inside a zone
        for tid, s in sessions.items():
            if not s.closed and tid not in active and (fidx - s.last_seen_frame) > stale:
                if s.last_zone is not None:
                    dwell = (s.last_seen_frame - s.zone_enter_frame) / fps * 1000
                    sink.emit(camera_id, s.visitor_id, "ZONE_EXIT",
                              iso(start_dt, s.last_seen_frame, fps),
                              zone_id=s.last_zone, dwell_ms=dwell, is_staff=s.is_staff,
                              confidence=0.5, sku_zone=s.last_zone, session_seq=s.next_seq())
                s.closed = True
        fidx += 1

    # end of clip: close anyone still in a zone
    for s in sessions.values():
        if not s.closed and s.last_zone is not None:
            dwell = (s.last_seen_frame - s.zone_enter_frame) / fps * 1000
            sink.emit(camera_id, s.visitor_id, "ZONE_EXIT", iso(start_dt, s.last_seen_frame, fps),
                      zone_id=s.last_zone, dwell_ms=dwell, is_staff=s.is_staff,
                      confidence=0.5, sku_zone=s.last_zone, session_seq=s.next_seq())
    cap.release()


# --------------------------------------------------------------------------
# CAM6 -- BILLING_QUEUE_JOIN / ABANDON, debounced in seconds
# --------------------------------------------------------------------------

def process_billing(model, video, camera_id, start_dt, sink, queue_region=None):
    cap, fps, w, h = open_video(video)
    qzone = poly_px(queue_region or QUEUE_REGION, w, h)
    min_present = max(1, int(QUEUE_MIN_PRESENCE_S * fps / FRAME_STRIDE))
    min_absent = max(1, int(QUEUE_MIN_ABSENCE_S * fps / FRAME_STRIDE))

    # tid -> [present_streak, absent_streak, in_queue, join_frame, visitor, seq]
    st = {}
    fidx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fidx % FRAME_STRIDE:
            fidx += 1
            continue
        ts = iso(start_dt, fidx, fps)

        present = set()
        for box, tid, conf in track_frame(model, frame):
            foot = (int((box[0] + box[2]) / 2), int(box[3]))
            if in_poly(foot, qzone) and not is_pink_uniform(torso_crop(frame, box)):
                present.add(tid)
        queue_depth = len(present)

        for tid in present:
            s = st.setdefault(tid, [0, 0, False, 0, new_visitor(), 0])
            s[0] += 1
            s[1] = 0
        for tid, s in st.items():
            if tid not in present:
                s[1] += 1
                s[0] = 0

        # JOIN once presence is real and a queue already exists
        for tid, s in st.items():
            if not s[2] and s[0] >= min_present:
                s[2], s[3] = True, fidx
                if queue_depth > 1:
                    s[5] += 1
                    sink.emit(camera_id, s[4], "BILLING_QUEUE_JOIN", ts,
                              zone_id="BILLING", queue_depth=queue_depth,
                              confidence=0.7, sku_zone="BILLING", session_seq=s[5])
        # ABANDON once they've really left, and only if they actually queued
        for tid, s in st.items():
            if s[2] and s[1] >= min_absent:
                dwell_s = (fidx - s[3]) / fps
                s[2] = False
                if dwell_s >= QUEUE_MIN_DWELL_S:
                    s[5] += 1
                    sink.emit(camera_id, s[4], "BILLING_QUEUE_ABANDON", ts,
                              zone_id="BILLING", dwell_ms=dwell_s * 1000,
                              queue_depth=queue_depth, confidence=0.6,
                              sku_zone="BILLING", session_seq=s[5])
        fidx += 1
    cap.release()


# --------------------------------------------------------------------------
# debug renderer -- draw the tripwire + tracked boxes + ids + crossings onto
# an mp4 so we calibrate the line by *watching*, not guessing. Mirrors the
# crossing logic in process_entry so what you see is what the counter counts.
# --------------------------------------------------------------------------

def render_debug(clips_dir, clip_name, out_mp4):
    model = YOLO(MODEL)
    cap, fps, w, h = open_video(os.path.join(clips_dir, clip_name))
    line_y = int(ENTRY_LINE_FRAC * h)
    band = int(ENTRY_BAND_FRAC * h)
    cooldown = ENTRY_COOLDOWN_S * fps
    vw = cv2.VideoWriter(out_mp4, cv2.VideoWriter_fourcc(*"mp4v"),
                         fps / FRAME_STRIDE, (w, h))

    confirmed, seen, last_cross = {}, defaultdict(int), defaultdict(lambda: -1e9)
    n_in = n_out = fidx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fidx % FRAME_STRIDE:
            fidx += 1
            continue
        cv2.line(frame, (0, line_y), (w, line_y), (0, 0, 255), 2)
        cv2.line(frame, (0, line_y - band), (w, line_y - band), (0, 255, 255), 1)
        cv2.line(frame, (0, line_y + band), (w, line_y + band), (0, 255, 255), 1)

        for box, tid, conf in track_frame(model, frame):
            seen[tid] += 1
            x1, y1, x2, y2 = [int(v) for v in box]
            foot = (int((x1 + x2) / 2), y2)
            pink = is_pink_uniform(torso_crop(frame, box))
            col = (255, 0, 255) if pink else (0, 200, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
            cv2.circle(frame, foot, 5, (0, 0, 255), -1)
            cv2.putText(frame, f"{tid}:{conf:.2f}{'*S' if pink else ''}", (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)

            side = "above" if y2 < line_y - band else "below" if y2 > line_y + band else None
            if side is None:
                continue
            prev = confirmed.get(tid)
            confirmed[tid] = side
            if prev and prev != side and seen[tid] >= MIN_TRACK_FRAMES \
                    and fidx - last_cross[tid] >= cooldown:
                last_cross[tid] = fidx
                if prev == "above":
                    n_in += 1
                    cv2.putText(frame, "IN", foot, cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
                else:
                    n_out += 1
                    cv2.putText(frame, "OUT", foot, cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)

        cv2.putText(frame, f"IN {n_in}  OUT {n_out}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 3)
        vw.write(frame)
        fidx += 1
    vw.release()
    cap.release()
    print(f"  wrote {out_mp4}  ->  IN={n_in}  OUT={n_out}")


# --------------------------------------------------------------------------
# driver + QA report
# --------------------------------------------------------------------------

def run(clips_dir, out_path="events.jsonl", layout_path=None):
    model = YOLO(MODEL)
    sink = EventSink(out_path)
    reid_gallery = []
    floor_zones, queue_region = load_layout(layout_path)
    for fname, cam, role, start in CLIPS:
        path = os.path.join(clips_dir, fname)
        if not os.path.exists(path):
            print(f"  skip (missing): {fname}")
            continue
        start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        print(f"  {fname:18s} -> {cam} ({role})")
        if role == "entry":
            process_entry(model, path, cam, start_dt, sink, reid_gallery)
        elif role == "floor":
            process_floor(model, path, cam, start_dt, sink, floor_zones)
        elif role == "billing":
            process_billing(model, path, cam, start_dt, sink, queue_region)
    sink.close()
    _report(out_path)


def _report(out_path):
    from collections import Counter
    rows = [json.loads(l) for l in open(out_path)]
    by_type = Counter(r["event_type"] for r in rows)
    by_cam = Counter(r["camera_id"] for r in rows)
    ids = [r["event_id"] for r in rows]
    print("\n" + "=" * 52)
    print(f"  events written : {len(rows)}")
    print(f"  unique event_id: {len(set(ids))}  (dupes: {len(ids)-len(set(ids))})")
    print(f"  unique visitors: {len({r['visitor_id'] for r in rows})}")
    print(f"  staff-flagged  : {sum(r['is_staff'] for r in rows)}")
    print("  by event_type  :", dict(by_type))
    print("  by camera      :", dict(by_cam))
    print(f"  entries        : {by_type.get('ENTRY',0)}   "
          f"exits: {by_type.get('EXIT',0)}   reentries: {by_type.get('REENTRY',0)}")
    print("=" * 52)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True)
    ap.add_argument("--out", default="events.jsonl")
    ap.add_argument("--layout", default=None,
                    help="optional store_layout.json; zones fall back to "
                         "the calibrated built-ins when omitted")
    args = ap.parse_args()
    run(args.clips, args.out, args.layout)
