#!/usr/bin/env python3
"""Short C920 UVC access and capture probe for phase 6.1.

Run from the privileged PYNQ Jupyter kernel. It does not load/program an FPGA
overlay, change camera controls, or save image content. The USB descriptors are
inventoried separately; this checks actual V4L2 open/format negotiation and a
short frame read for both streaming nodes and common 640x480 encodings.
"""

import hashlib
import json
import os
import time

import cv2


def fourcc_text(value):
    value = int(value)
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


def probe(index, requested_fourcc):
    result = {
        "device": "/dev/video{}".format(index),
        "requested_fourcc": requested_fourcc,
        "requested_width": 640,
        "requested_height": 480,
        "requested_fps": 30,
    }
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    result["opened"] = bool(cap.isOpened())
    if not result["opened"]:
        cap.release()
        return result

    try:
        result["set_fourcc"] = bool(cap.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*requested_fourcc),
        ))
        result["set_width"] = bool(cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640))
        result["set_height"] = bool(cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480))
        result["set_fps"] = bool(cap.set(cv2.CAP_PROP_FPS, 30))
        result["actual_fourcc"] = fourcc_text(cap.get(cv2.CAP_PROP_FOURCC))
        result["actual_width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        result["actual_height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        result["actual_fps"] = cap.get(cv2.CAP_PROP_FPS)

        frames = []
        deadline = time.monotonic() + 8.0
        while len(frames) < 5 and time.monotonic() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        result["frames_read"] = len(frames)
        if frames:
            last = frames[-1]
            result["frame_shape"] = list(last.shape)
            result["frame_dtype"] = str(last.dtype)
            result["last_frame_sha256"] = hashlib.sha256(last.tobytes()).hexdigest()
            result["last_frame_stddev"] = float(last.std())
        else:
            result["frame_read_error"] = "No frame returned within 8 seconds"
    finally:
        cap.release()
    return result


report = {
    "board": os.uname().nodename,
    "opencv_version": cv2.__version__,
    "devices": [],
}
for node in (0, 1):
    for encoding in ("MJPG", "YUYV"):
        try:
            report["devices"].append(probe(node, encoding))
        except Exception as exc:
            report["devices"].append({
                "device": "/dev/video{}".format(node),
                "requested_fourcc": encoding,
                "exception": "{}: {}".format(type(exc).__name__, exc),
            })

print(json.dumps(report, indent=2, sort_keys=True))
