#!/usr/bin/env bash
# Process the store's CCTV into behavioural events, then optionally stream them
# into the API. Run from the repo root.
#
# The camera->video mapping, fps, and clip start times live in data/store_layout.json
# (the `video`/`fps`/`start` fields), so this needs no clips-dir convention. Drop the
# challenge footage at ../CCTV Footage/ (paths are relative to the layout file).
#
#   bash pipeline/run.sh                              # -> events/ST1008.jsonl
#   VID_STRIDE=5 bash pipeline/run.sh                 # finer temporal sampling
#   API_URL=http://localhost:8000 bash pipeline/run.sh   # also replay into the API
#
# If the footage is absent, falls back to the simulator for a footage-free demo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYOUT="${LAYOUT:-$ROOT/data/store_layout.json}"
STORE="${STORE:-ST1008}"
OUT_DIR="${OUT_DIR:-$ROOT/events}"
MODEL="${MODEL:-yolov8n.pt}"
VID_STRIDE="${VID_STRIDE:-5}"
PY="${PYTHON:-python}"

mkdir -p "$OUT_DIR"

# detect.py resolves each camera's `video` from the layout; if none are found it prints
# "video not found" for every camera and emits 0 events -> use the simulator instead.
set +e
"$PY" "$ROOT/pipeline/detect.py" \
  --store "$STORE" --layout "$LAYOUT" --model "$MODEL" \
  --vid-stride "$VID_STRIDE" --out "$OUT_DIR/$STORE.jsonl"
rc=$?
set -e

if [ "$rc" -ne 0 ] || [ ! -s "$OUT_DIR/$STORE.jsonl" ]; then
  echo
  echo "No events produced (footage missing or pipeline unavailable)."
  echo "For a footage-free demo, run the simulator instead:"
  echo "    $PY tools/simulate.py --api \${API_URL:-http://localhost:8000} --live --store $STORE"
  exit 0
fi

echo "Wrote $OUT_DIR/$STORE.jsonl"

if [ -n "${API_URL:-}" ]; then
  echo ">> replaying events into $API_URL"
  "$PY" "$ROOT/tools/simulate.py" --api "$API_URL" --from-file "$OUT_DIR/$STORE.jsonl"
fi
