#!/usr/bin/env python3
"""Measure sustained C920 UVC capture without importing PYNQ.

This measures the camera-to-OpenCV path only. Long frame gaps are reported as
possible stalls; OpenCV does not expose the V4L2 sequence counter needed for an
exact dropped-frame count. HDMI transport and end-to-end latency are not
measured here.
"""

import argparse
import json
import os
import time
from pathlib import Path

import cv2


def fourcc_text(value):
    value = int(value)
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


def summary_ms(values):
    if not values:
        return None
    values = sorted(values)
    return {
        "min": round(values[0], 3),
        "median": round(values[len(values) // 2], 3),
        "p95": round(values[int((len(values) - 1) * 0.95)], 3),
        "max": round(values[-1], 3),
    }


def measure(args):
    cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("Could not open {}".format(args.device))

    try:
        requested = (
            (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc)),
            (cv2.CAP_PROP_FRAME_WIDTH, args.width),
            (cv2.CAP_PROP_FRAME_HEIGHT, args.height),
            (cv2.CAP_PROP_FPS, args.fps),
            (cv2.CAP_PROP_BUFFERSIZE, 1),
        )
        set_ok = [bool(cap.set(prop, value)) for prop, value in requested]
        mode = {
            "fourcc": fourcc_text(cap.get(cv2.CAP_PROP_FOURCC)),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "reported_fps": float(cap.get(cv2.CAP_PROP_FPS)),
            "set_calls_succeeded": set_ok,
        }
        if (mode["fourcc"], mode["width"], mode["height"]) != (
            args.fourcc, args.width, args.height
        ):
            raise RuntimeError("Camera did not accept requested mode: {}".format(mode))

        warmup_deadline = time.monotonic() + args.warmup_timeout
        frame = None
        while time.monotonic() < warmup_deadline:
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
                break
        if frame is None:
            raise RuntimeError("No frame received during warmup")

        timestamps = []
        errors = 0
        last_frame = frame
        start = time.monotonic()
        deadline = start + args.duration
        while time.monotonic() < deadline:
            ok, candidate = cap.read()
            returned = time.monotonic()
            if not ok or candidate is None:
                errors += 1
                continue
            timestamps.append(returned)
            last_frame = candidate

        elapsed = time.monotonic() - start
        intervals_ms = [
            (timestamps[i] - timestamps[i - 1]) * 1000.0
            for i in range(1, len(timestamps))
        ]
        fps = len(timestamps) / elapsed if elapsed > 0 else 0.0
        nominal_fps = mode["reported_fps"] or args.fps
        gap_limit_ms = 1500.0 / nominal_fps
        long_gaps = sum(interval > gap_limit_ms for interval in intervals_ms)

        preview_saved = False
        if args.preview:
            preview = Path(args.preview)
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview_saved = bool(cv2.imwrite(str(preview), last_frame))

        return {
            "tool": "phase6_uvc_perf",
            "scope": "UVC/V4L2 capture only; no PYNQ, PL, or HDMI",
            "board": os.uname().nodename,
            "opencv_version": cv2.__version__,
            "device": args.device,
            "requested_mode": {
                "fourcc": args.fourcc,
                "width": args.width,
                "height": args.height,
                "fps": args.fps,
            },
            "camera_mode": mode,
            "duration_s": args.duration,
            "frames_read": len(timestamps),
            "observed_fps": round(fps, 3),
            "read_errors": errors,
            "inter_frame_interval_ms": summary_ms(intervals_ms),
            "long_gaps": {
                "count": long_gaps,
                "threshold_ms": round(gap_limit_ms, 3),
                "meaning": "possible stalls, not an exact dropped-frame count",
            },
            "exact_dropped_frame_count": None,
            "latency": None,
            "preview_path": args.preview,
            "preview_saved": preview_saved,
        }
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--fourcc", choices=("MJPG", "YUYV"), default="MJPG")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--warmup-timeout", type=float, default=8.0)
    parser.add_argument("--preview", help="optional path to save the last frame")
    parser.add_argument("--output", help="optional path for the JSON report")
    args = parser.parse_args()

    try:
        report = measure(args)
    except Exception as exc:
        report = {"tool": "phase6_uvc_perf", "error": "{}: {}".format(
            type(exc).__name__, exc
        )}

    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if "error" not in report else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
