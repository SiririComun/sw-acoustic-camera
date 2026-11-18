#!/usr/bin/env python3
"""Show the live C920 feed on HDMI OUT, without using HDMI IN.

Run on the PYNQ-Z2 with the C920 connected over USB and a display connected
to HDMI OUT. This isolates UVC capture, PS frame handling, and BaseOverlay
HDMI output from the HDMI-IN capture/readback path.
"""

import argparse
import json
import time
import traceback

import phase6_video_perf as phase6


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=30.0,
                        help="seconds to display video (default: 30)")
    args = parser.parse_args()
    if args.duration < 3:
        parser.error("--duration must be at least 3 seconds")

    result = {
        "board": "PYNQ-Z2",
        "duration_s": args.duration,
        "camera_node": "/dev/video0",
        "mode": {"width": 640, "height": 480, "fourcc": "MJPG",
                 "camera_fps_requested": 30, "hdmi_fps_requested": 60},
        "frames_read": 0,
        "frames_sent": 0,
        "capture_read_failures": 0,
        "application_frame_queue": "none; synchronous read-copy-write path",
        "camera_driver_sequence_drop_count_available": False,
    }
    camera = None
    hdmi_out = None
    out_started = False
    capture_frame_intervals_ms = []
    read_return_to_writeframe_return_ms = []
    writeframe_call_ms = []
    previous_read_return_ns = None
    start = None
    try:
        result["base_artifacts"] = phase6.verify_base_artifacts()
        phase6.use_legacy_metadata_parser()
        overlay = phase6.Overlay(
            str(phase6.BASE_BIT), download=True,
            device=phase6.Device.active_device,
        )
        result["base_load_proof"] = phase6.verify_loaded_base()
        if not overlay.is_loaded():
            raise RuntimeError("Official PYNQ-Z2 base overlay did not load")

        camera = phase6.cv2.VideoCapture(phase6.CAMERA_NODE, phase6.cv2.CAP_V4L2)
        if not camera.isOpened():
            raise RuntimeError("OpenCV could not open /dev/video0")
        camera.set(phase6.cv2.CAP_PROP_FOURCC,
                   phase6.cv2.VideoWriter_fourcc(*"MJPG"))
        camera.set(phase6.cv2.CAP_PROP_FRAME_WIDTH, phase6.WIDTH)
        camera.set(phase6.cv2.CAP_PROP_FRAME_HEIGHT, phase6.HEIGHT)
        camera.set(phase6.cv2.CAP_PROP_FPS, phase6.CAMERA_FPS)
        result["camera_mode"] = {
            "actual_fourcc": phase6.fourcc_text(
                camera.get(phase6.cv2.CAP_PROP_FOURCC)),
            "actual_width": int(camera.get(phase6.cv2.CAP_PROP_FRAME_WIDTH)),
            "actual_height": int(camera.get(phase6.cv2.CAP_PROP_FRAME_HEIGHT)),
            "reported_fps": float(camera.get(phase6.cv2.CAP_PROP_FPS)),
        }
        if (result["camera_mode"]["actual_fourcc"] != "MJPG"
                or result["camera_mode"]["actual_width"] != phase6.WIDTH
                or result["camera_mode"]["actual_height"] != phase6.HEIGHT):
            raise RuntimeError("C920 did not negotiate MJPG 640x480")

        hdmi_out = overlay.video.hdmi_out
        hdmi_out.cacheable_frames = False
        hdmi_out.configure(
            phase6.VideoMode(phase6.WIDTH, phase6.HEIGHT, 24, phase6.HDMI_FPS)
        )
        hdmi_out.start()
        out_started = True
        phase6.trace("HDMI OUT active; displaying C920 frames")

        start = time.monotonic()
        deadline = start + args.duration
        while time.monotonic() < deadline:
            ok, frame = camera.read()
            read_return_ns = time.monotonic_ns()
            if not ok or frame is None:
                result["capture_read_failures"] += 1
                raise RuntimeError("C920 stopped returning frames")
            result["frames_read"] += 1
            if previous_read_return_ns is not None:
                capture_frame_intervals_ms.append(
                    (read_return_ns - previous_read_return_ns) / 1_000_000
                )
            previous_read_return_ns = read_return_ns
            if frame.shape[:2] != (phase6.HEIGHT, phase6.WIDTH):
                raise RuntimeError(f"Unexpected C920 frame shape {frame.shape}")
            out_frame = hdmi_out.newframe()
            phase6.np.asarray(out_frame)[...] = frame
            write_start_ns = time.monotonic_ns()
            hdmi_out.writeframe(out_frame)
            write_return_ns = time.monotonic_ns()
            read_return_to_writeframe_return_ms.append(
                (write_return_ns - read_return_ns) / 1_000_000
            )
            writeframe_call_ms.append((write_return_ns - write_start_ns) / 1_000_000)
            result["frames_sent"] += 1

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        phase6.trace(f"output preview failed: {type(exc).__name__}: {exc}")
    finally:
        result["application_frames_not_submitted"] = max(
            result["frames_read"] - result["frames_sent"], 0
        )
        if start is not None:
            elapsed = time.monotonic() - start
            result["elapsed_s"] = round(elapsed, 3)
            result["camera_read_fps"] = round(result["frames_read"] / elapsed, 3)
            result["hdmi_writeframe_fps"] = round(result["frames_sent"] / elapsed, 3)
            result["capture_frame_interval_ms"] = phase6.stats_summary(
                capture_frame_intervals_ms
            )
            result["camera_return_to_hdmi_writeframe_return_ms"] = (
                phase6.stats_summary(read_return_to_writeframe_return_ms)
            )
            result["hdmi_writeframe_call_ms"] = phase6.stats_summary(
                writeframe_call_ms
            )
            result["output_fps"] = result["hdmi_writeframe_fps"]
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        if camera is not None:
            camera.release()
        if out_started:
            try:
                hdmi_out.close()
            except Exception as exc:
                result["hdmi_out_cleanup_error"] = f"{type(exc).__name__}: {exc}"
    if result.get("error") or not result["frames_sent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    phase6.faulthandler.enable()
    main()
