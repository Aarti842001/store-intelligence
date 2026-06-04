#!/usr/bin/env bash
# One command to turn the raw clips into events.jsonl.
#
#   ./pipeline/run.sh "/path/to/Store 2"  [output.jsonl]  [store_layout.json]
#
# The detection pipeline is GPU-friendly (we develop it on a Colab T4) but falls
# back to CPU automatically. If a store_layout.json is passed (or sits next to
# the clips), zone names are read from it; otherwise the calibrated built-in
# zones are used. See docs/CHOICES.md for the model rationale and
# pipeline/detect_store.py for the per-camera logic.
set -e

CLIPS="${1:?usage: run.sh <clips_dir> [out.jsonl] [store_layout.json]}"
OUT="${2:-events.jsonl}"
# 3rd arg wins; otherwise auto-pick a store_layout.json sitting beside the clips
LAYOUT="${3:-$CLIPS/store_layout.json}"
[ -f "$LAYOUT" ] || LAYOUT=""

python - "$CLIPS" "$OUT" "$LAYOUT" <<'PY'
import sys
sys.path.insert(0, "pipeline")
import detect_store
detect_store.run(sys.argv[1], sys.argv[2], sys.argv[3] or None)
PY

echo "events written to $OUT"
echo "feed them in with:  python scripts/replay.py $OUT --batch"
