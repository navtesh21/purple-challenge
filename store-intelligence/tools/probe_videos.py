"""Probe the real CCTV videos and dump sample frames for calibration.

For each CAM*.mp4: print resolution, fps, frame count, duration, and write JPEG
frames at several positions so we can eyeball the scene (entry vs floor vs billing),
zone regions, and any burned-in timestamp for wall-clock alignment with the POS log.

Usage: python tools/probe_videos.py
Outputs frames to tools/frames/<cam>_<pct>.jpg
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2

FOOTAGE = Path(r"D:\purple\CCTV Footage")
OUT = Path(__file__).resolve().parent / "frames"
OUT.mkdir(exist_ok=True)
SAMPLE_PCTS = [0.02, 0.25, 0.5, 0.75, 0.98]


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"file": path.name, "error": "could not open"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    dur = n / fps if fps else 0
    stem = path.stem.replace(" ", "_")
    saved = []
    for pct in SAMPLE_PCTS:
        idx = int(n * pct)
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            # downscale for quick viewing
            fh, fw = frame.shape[:2]
            scale = 900 / max(fw, 1)
            if scale < 1:
                frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)))
            out = OUT / f"{stem}_{int(pct*100):02d}.jpg"
            cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            saved.append(out.name)
    cap.release()
    return {"file": path.name, "w": w, "h": h, "fps": round(fps, 2),
            "frames": n, "duration_s": round(dur, 1), "saved": saved}


if __name__ == "__main__":
    vids = sorted(FOOTAGE.glob("*.mp4"))
    report = [probe(v) for v in vids]
    print(json.dumps(report, indent=2))
    (OUT / "_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
