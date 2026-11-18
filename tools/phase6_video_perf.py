#!/usr/bin/env python3
"""Measure the C920 -> PYNQ HDMI OUT -> HDMI IN loopback path.

Run as root on the PYNQ-Z2 with the C920 on USB and HDMI OUT connected to
HDMI IN. The script explicitly reloads the verified official PYNQ-Z2 base.bit
before opening its video IP. It does not save camera images.

The latency is measured from OpenCV returning a camera frame (and, separately,
from submitting that frame to HDMI OUT) until HDMI IN returns that same frame,
identified by a small software sequence marker. It excludes sensor exposure
and any camera/driver buffering before OpenCV returns the frame.

Run one camera format per process. The HDMI cores are initialized and closed
once per process; reinitializing HDMI after closing it has not been reliable on
the tested PYNQ image.
"""

import argparse
import asyncio
import faulthandler
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
import traceback
from pathlib import Path
from queue import Empty, Full, Queue

def import_trace(stage):
    if os.environ.get("PHASE6_TRACE") == "1":
        print(f"[phase6-import] {stage}", file=sys.stderr, flush=True)


faulthandler.enable()
import_trace("before pynq")
# PYNQ imports its optional PMBus/sensor module at package initialization.
# That module invokes CFFI/pycparser in this image and currently segfaults;
# this video-only probe does not use the PMBus API, so skip it in-process.
def _phase6_pmbus_disabled(*args, **kwargs):
    raise RuntimeError("PMBus is disabled in the phase 6 video probe")


pmbus_stub = types.ModuleType("pynq.pmbus")
pmbus_stub.DataRecorder = _phase6_pmbus_disabled
pmbus_stub.get_rails = _phase6_pmbus_disabled
sys.modules["pynq.pmbus"] = pmbus_stub
import_trace("optional pynq.pmbus import skipped")
import pynq
import_trace("pynq loaded")
import pynq.lib.video  # Register video drivers before constructing Overlay.
import_trace("pynq.lib.video loaded")
from pynq import Overlay
from pynq.lib.video import VideoMode
import_trace("overlay/video imports loaded")
from pynq.pl_server.device import Device
from pynq.pl_server.embedded_device import (
    EmbeddedDevice,
    _create_xclbin,
    _unify_dictionaries,
    bit2bin,
)
from pynq.pl_server.hwh_parser import HWH
from pynq.pl_server.xclbin_parser import XclBin
import_trace("all PYNQ imports loaded")
import numpy as np
import_trace("numpy loaded")
import cv2
import_trace("cv2 loaded")


BASE_BIT = Path(pynq.__file__).parent / "overlays/base/base.bit"
BASE_BIN = Path("/lib/firmware/base.bin")
STATE_FILE = Path(pynq.__file__).parent / "pl_server/global_pl_state_.json"
FPGA_STATE = Path("/sys/class/fpga_manager/fpga0/state")
FPGA_FLAGS = Path("/sys/class/fpga_manager/fpga0/flags")
EXPECTED_BASE_BIT_SHA256 = "3239e9cc37ee4993c25f02ae6c97da0b3372f0e722e267d10073e20adc5d6717"
EXPECTED_BASE_HWH_SHA256 = "222898ef2c0ccc4a0c47608e99d8dc7c9c6cdfed5130964884ff35005827ef3a"

CAMERA_NODE = 0
WIDTH = 640
HEIGHT = 480
CAMERA_FPS = 30
HDMI_FPS = 60
MARKER_MAGIC = 0b1101
MARKER_BITS = 16  # 4-bit sync plus a 12-bit camera frame sequence.
MARKER_X = 12
MARKER_Y = 12
MARKER_CELL_W = 20
MARKER_CELL_H = 24
MARKER_PITCH = 28


def trace(message):
    if os.environ.get("PHASE6_TRACE") == "1":
        print(f"[phase6-trace] {message}", file=sys.stderr, flush=True)


def fourcc_text(value):
    value = int(value)
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


def verify_base_artifacts():
    bit_data = BASE_BIT.read_bytes()
    hwh_data = BASE_BIT.with_suffix(".hwh").read_bytes()
    bit_hash = hashlib.sha256(bit_data).hexdigest()
    hwh_hash = hashlib.sha256(hwh_data).hexdigest()
    if bit_hash != EXPECTED_BASE_BIT_SHA256 or hwh_hash != EXPECTED_BASE_HWH_SHA256:
        raise RuntimeError("Installed official base.bit/base.hwh do not match the verified PYNQ-Z2 image")
    return {"base_bit_sha256": bit_hash, "base_hwh_sha256": hwh_hash}


def use_legacy_metadata_parser():
    """Apply the known PYNQ 3.1 HWH workaround in this process only."""

    def parse_hwh(self, bitfile_name, partial=False):
        parser = HWH(hwh_name=str(Path(bitfile_name).with_suffix(".hwh")))
        parser.xclbin_data = _create_xclbin(parser.mem_dict)
        xclbin_parser = XclBin(xclbin_data=parser.xclbin_data)
        _unify_dictionaries(parser, xclbin_parser)
        parser.bin_data = bit2bin(Path(bitfile_name).read_bytes())
        parser.dtbo_data = None
        parser.systemgraph = None
        if "PSDDR" not in parser.mem_dict or parser.mem_dict["PSDDR"].get("base_address") != 0:
            raise RuntimeError("Synthetic XRT metadata did not expose PS DDR")
        return parser

    EmbeddedDevice.get_bitfile_metadata = parse_hwh


def verify_loaded_base():
    manager_state = FPGA_STATE.read_text(encoding="ascii").strip()
    manager_flags = FPGA_FLAGS.read_text(encoding="ascii").strip()
    if manager_state != "operating" or manager_flags not in ("0", "0x0"):
        raise RuntimeError(
            f"FPGA manager state is {manager_state!r}, flags {manager_flags!r}; expected operating/0"
        )
    bit_data = BASE_BIT.read_bytes()
    bin_data = BASE_BIN.read_bytes()
    if bit2bin(bit_data) != bin_data:
        raise RuntimeError("/lib/firmware/base.bin does not match official base.bit")
    log = subprocess.run(["dmesg"], check=True, capture_output=True, text=True).stdout
    loads = re.findall(r"fpga_manager fpga0: writing (\S+) to Xilinx Zynq FPGA Manager", log)
    if not loads or Path(loads[-1]).name != BASE_BIN.name:
        raise RuntimeError("Latest FPGA-manager load was not base.bin")
    if not STATE_FILE.exists():
        raise RuntimeError("PYNQ global PL state file is missing after base.bit download")
    global_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if (Path(global_state.get("bitfile_name", "")).resolve() != BASE_BIT.resolve()
            or global_state.get("bitfile_hash") != hashlib.sha1(bit_data).hexdigest()):
        raise RuntimeError("PYNQ global PL state does not identify the official base.bit")
    return {
        "manager_state": manager_state,
        "manager_flags": manager_flags,
        "latest_fpga_manager_load": loads[-1],
        "base_bin_matches_base_bit": True,
        "pynq_global_state_matches_base_bit": True,
    }


def draw_marker(frame, sequence):
    """Draw a robust high-contrast marker after UVC capture, before HDMI OUT."""
    bits = [(MARKER_MAGIC >> shift) & 1 for shift in range(3, -1, -1)]
    bits.extend((sequence >> shift) & 1 for shift in range(11, -1, -1))
    frame[MARKER_Y - 4:MARKER_Y + MARKER_CELL_H + 4,
          MARKER_X - 4:MARKER_X + MARKER_PITCH * MARKER_BITS, :] = 127
    for index, bit in enumerate(bits):
        x0 = MARKER_X + index * MARKER_PITCH
        value = 255 if bit else 0
        frame[MARKER_Y:MARKER_Y + MARKER_CELL_H,
              x0:x0 + MARKER_CELL_W, :] = value
    return frame


def decode_marker(frame):
    # Keep this as a view of the PYNQ DMA buffer. The synchronous PYNQ
    # readframe() path has already passed full-array statistics in the HDMI
    # loopback smoke test. Avoid copying the DMA allocation.
    image = np.asarray(frame)
    if image.ndim != 3 or image.shape[0] < 48 or image.shape[1] < 470:
        return None
    bits = []
    for index in range(MARKER_BITS):
        x0 = MARKER_X + index * MARKER_PITCH
        y0 = MARKER_Y + 4
        y1 = MARKER_Y + MARKER_CELL_H - 4
        x1 = x0 + MARKER_CELL_W
        bits.append(int(float(image[y0:y1, x0:x1, 0].mean()) > 127))
    magic = 0
    sequence = 0
    for bit in bits[:4]:
        magic = (magic << 1) | bit
    if magic != MARKER_MAGIC:
        return None
    for bit in bits[4:]:
        sequence = (sequence << 1) | bit
    return sequence


def stats_summary(values):
    if not values:
        return None
    data = np.asarray(values, dtype=np.float64)
    return {
        "count": int(data.size),
        "min_ms": round(float(np.min(data)), 3),
        "median_ms": round(float(np.median(data)), 3),
        "p95_ms": round(float(np.percentile(data, 95)), 3),
        "max_ms": round(float(np.max(data)), 3),
    }


def run_mode(fourcc, duration_s, video, preview_path=None):
    trace(f"{fourcc}: opening camera")
    result = {
        "requested_mode": {
            "fourcc": fourcc,
            "width": WIDTH,
            "height": HEIGHT,
            "fps": CAMERA_FPS,
        },
        "hdmi_mode": {
            "width": WIDTH,
            "height": HEIGHT,
            "bits_per_pixel": 24,
            "requested_fps": HDMI_FPS,
        },
        "duration_s": duration_s,
        "camera_node": f"/dev/video{CAMERA_NODE}",
        "camera_readback_latency_includes": "OpenCV-return-to-HDMI-IN-read; excludes sensor exposure and camera/driver buffering before OpenCV returns",
    }
    cap = cv2.VideoCapture(CAMERA_NODE, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        result["error"] = "OpenCV could not open /dev/video0"
        return result

    hdmi_in = video.hdmi_in
    hdmi_out = video.hdmi_out
    hdmi_in.cacheable_frames = False
    hdmi_out.cacheable_frames = False
    capture_queue = Queue(maxsize=1)
    stop_capture = threading.Event()
    lock = threading.Lock()
    capture_stats = {"frames": 0, "errors": 0, "overwrites": 0,
                     "start": None, "end": None}
    rx_stats = {
        "frames": 0,
        "read_timeouts": 0,
        "untagged": 0,
        "repeat_tags": 0,
        "unique_sequences": set(),
        "read_times": [],
        "first_frame_shape": None,
        "first_frame_dtype": None,
        "latency_from_capture_ms": [],
        "latency_from_submit_ms": [],
        "reader_error": None,
    }
    sent = {}
    submitted_sequences = []

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    buffer_size_set = bool(cap.set(cv2.CAP_PROP_BUFFERSIZE, 1))
    result["camera_mode"] = {
        "actual_fourcc": fourcc_text(cap.get(cv2.CAP_PROP_FOURCC)),
        "actual_width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "actual_height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "reported_fps": float(cap.get(cv2.CAP_PROP_FPS)),
        "set_buffer_size_one": buffer_size_set,
        "reported_buffer_size": float(cap.get(cv2.CAP_PROP_BUFFERSIZE)),
    }

    if (result["camera_mode"]["actual_fourcc"] != fourcc
            or result["camera_mode"]["actual_width"] != WIDTH
            or result["camera_mode"]["actual_height"] != HEIGHT):
        cap.release()
        result["error"] = "Requested C920 mode was not negotiated as requested"
        return result

    out_started = in_started = False
    capture_thread = None
    input_loop = None

    def read_input_frame(timeout_s=3.0):
        # Use the documented async Base API with a deadline so a missing HDMI
        # frame is reported instead of leaving the process blocked forever.
        try:
            return input_loop.run_until_complete(
                asyncio.wait_for(hdmi_in.readframe_async(), timeout=timeout_s)
            )
        except asyncio.TimeoutError:
            rx_stats["read_timeouts"] += 1
            raise

    try:
        input_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(input_loop)
        hdmi_out.configure(VideoMode(WIDTH, HEIGHT, 24, HDMI_FPS))
        hdmi_out.start()
        out_started = True
        trace(f"{fourcc}: HDMI OUT started")
        # Match the known-good 6.2 startup: keep the output VDMA fed with
        # several real frames before HDMI IN detects and starts capturing.
        priming_frames_sent = 0
        for _ in range(3):
            ok, prime_frame = cap.read()
            if not ok or prime_frame is None:
                raise RuntimeError("C920 did not return an HDMI priming frame")
            if prime_frame.shape[:2] != (HEIGHT, WIDTH):
                raise RuntimeError(
                    f"Unexpected C920 priming frame shape {prime_frame.shape}"
                )
            prime_out = hdmi_out.newframe()
            np.asarray(prime_out)[...] = prime_frame
            hdmi_out.writeframe(prime_out)
            priming_frames_sent += 1
        result["camera_priming_frames_sent"] = priming_frames_sent
        trace(f"{fourcc}: sent {priming_frames_sent} C920 priming frames to HDMI OUT")
        time.sleep(0.5)
        trace(f"{fourcc}: configuring HDMI IN; waiting for receiver lock")
        hdmi_in.configure()
        trace(f"{fourcc}: HDMI IN configure returned; reading mode")
        input_mode = hdmi_in.mode
        trace(f"{fourcc}: HDMI IN configured at {input_mode.width}x{input_mode.height}")
        result["hdmi_input_mode"] = {
            "width": input_mode.width,
            "height": input_mode.height,
            "bits_per_pixel": input_mode.bits_per_pixel,
        }
        hdmi_in.start()
        in_started = True
        trace(f"{fourcc}: HDMI IN started")
        # PYNQ documents that the first frame after a long pause may be stale.
        warmup_frame = read_input_frame()
        warmup_frame.freebuffer()
        trace(f"{fourcc}: discarded HDMI IN warm-up frame")

        def camera_worker():
            sequence = 0
            capture_stats["start"] = time.monotonic()
            while not stop_capture.is_set():
                ok, frame = cap.read()
                read_return = time.monotonic()
                if not ok or frame is None:
                    capture_stats["errors"] += 1
                    continue
                try:
                    capture_queue.put_nowait((sequence, frame, read_return))
                except Full:
                    try:
                        capture_queue.get_nowait()
                    except Empty:
                        pass
                    capture_stats["overwrites"] += 1
                    capture_queue.put_nowait((sequence, frame, read_return))
                sequence += 1
                capture_stats["frames"] = sequence
            capture_stats["end"] = time.monotonic()

        def consume_input_frame():
            # Use PYNQ's asynchronous Base API behind a bounded synchronous
            # wait so a missing frame becomes a reportable timeout.
            frame = read_input_frame()
            returned = time.monotonic()
            try:
                shape = list(frame.shape)
                dtype = str(frame.dtype)
                seq = decode_marker(frame)
            finally:
                frame.freebuffer()
            rx_stats["frames"] += 1
            rx_stats["read_times"].append(returned)
            if rx_stats["first_frame_shape"] is None:
                rx_stats["first_frame_shape"] = shape
                rx_stats["first_frame_dtype"] = dtype
            if seq is None:
                rx_stats["untagged"] += 1
            elif seq in rx_stats["unique_sequences"]:
                rx_stats["repeat_tags"] += 1
            else:
                rx_stats["unique_sequences"].add(seq)
                stamp = sent.get(seq)
                if stamp is not None:
                    rx_stats["latency_from_capture_ms"].append(
                        (returned - stamp["capture_return"]) * 1000.0
                    )
                    rx_stats["latency_from_submit_ms"].append(
                        (returned - stamp["submit"]) * 1000.0
                    )

        capture_thread = threading.Thread(target=camera_worker, name="c920-capture", daemon=True)
        capture_thread.start()
        trace(f"{fourcc}: C920 capture thread started")
        start = time.monotonic()
        deadline = start + duration_s
        output_errors = []
        last_trace = start
        while time.monotonic() < deadline:
            # Feed captured camera frames before waiting for their loopback.
            # Otherwise the three startup/blank VDMA buffers can be drained
            # before the first C920 frame is submitted, leaving HDMI IN idle.
            try:
                sequence, camera_frame, capture_return = capture_queue.get_nowait()
            except Empty:
                pass
            else:
                if camera_frame.shape[:2] != (HEIGHT, WIDTH):
                    output_errors.append(
                        f"Unexpected camera frame shape {camera_frame.shape}"
                    )
                else:
                    # OpenCV and PYNQ's HDMI video front-end both use BGR ordering.
                    camera_frame = draw_marker(camera_frame.copy(), sequence & 0xFFF)
                    out_frame = hdmi_out.newframe()
                    np.asarray(out_frame)[...] = camera_frame
                    submit = time.monotonic()
                    with lock:
                        sent[sequence & 0xFFF] = {
                            "capture_return": capture_return,
                            "submit": submit,
                            "source_sequence": sequence,
                        }
                        submitted_sequences.append(sequence & 0xFFF)
                    try:
                        hdmi_out.writeframe(out_frame)
                    except Exception as exc:
                        output_errors.append(f"{type(exc).__name__}: {exc}")
                        with lock:
                            sent.pop(sequence & 0xFFF, None)
            try:
                consume_input_frame()
            except Exception as exc:
                rx_stats["reader_error"] = f"{type(exc).__name__}: {exc}"
                break
            now = time.monotonic()
            if now - last_trace >= 1.0:
                trace(
                    f"{fourcc}: t={now - start:.1f}s camera={capture_stats['frames']} "
                    f"submitted={len(submitted_sequences)} hdmi_in={rx_stats['frames']}"
                )
                last_trace = now

        result["output_elapsed_s"] = round(time.monotonic() - start, 3)
        trace(f"{fourcc}: measurement loop ended")
        stop_capture.set()
        capture_thread.join(timeout=2.0)
        trace(f"{fourcc}: capture thread joined={not capture_thread.is_alive()}")
        if output_errors:
            result["output_errors"] = output_errors
        # Drain one second of HDMI-IN frames so frames submitted near the end
        # of the measurement window can still be matched.
        drain_deadline = time.monotonic() + 1.0
        while time.monotonic() < drain_deadline:
            try:
                consume_input_frame()
            except Exception as exc:
                rx_stats["reader_error"] = f"{type(exc).__name__}: {exc}"
                break
        if preview_path is not None:
            result["preview_save_error"] = (
                "Full-frame copy from the HDMI-IN DMA buffer is disabled after "
                "a board-side segmentation fault; marker sampling remains enabled"
            )

        with lock:
            rx = dict(rx_stats)
            rx["unique_sequences"] = set(rx_stats["unique_sequences"])
            rx["read_times"] = list(rx_stats["read_times"])
            rx["latency_from_capture_ms"] = list(rx_stats["latency_from_capture_ms"])
            rx["latency_from_submit_ms"] = list(rx_stats["latency_from_submit_ms"])
            sent_copy = dict(sent)

        capture_start = capture_stats["start"] or start
        capture_end = capture_stats["end"] or time.monotonic()
        capture_elapsed = max(capture_end - capture_start, 1e-9)
        rx_times = rx["read_times"]
        rx_elapsed = max(rx_times[-1] - rx_times[0], 1e-9) if len(rx_times) > 1 else 0.0
        submitted_set = set(submitted_sequences)
        received_submitted = submitted_set & rx["unique_sequences"]
        missing = submitted_set - rx["unique_sequences"]
        readback_valid = rx["reader_error"] is None and rx["frames"] > 0
        result["metrics"] = {
            "camera_frames_read": capture_stats["frames"],
            "camera_read_errors": capture_stats["errors"],
            "camera_read_fps_measured": round(capture_stats["frames"] / capture_elapsed, 3),
            "application_queue_overwrites": capture_stats["overwrites"],
            "frames_submitted_to_hdmi": len(submitted_sequences),
            "hdmi_submit_fps": round(len(submitted_sequences) / max(result["output_elapsed_s"], 1e-9), 3),
            "hdmi_in_frames_read": rx["frames"],
            "hdmi_in_read_timeouts": rx["read_timeouts"],
            "hdmi_in_read_behavior": (
                "PYNQ asynchronous readframe with a 3-second per-frame deadline"
            ),
            "hdmi_in_read_fps_measured": round((rx["frames"] - 1) / rx_elapsed, 3) if rx_elapsed else None,
            "hdmi_in_untagged_frames": rx["untagged"],
            "hdmi_in_repeated_tag_reads": rx["repeat_tags"],
            "hdmi_readback_measurements_valid": readback_valid,
            "unique_submitted_frames_recovered": len(received_submitted),
            "submitted_frames_not_seen_at_hdmi_in": len(missing) if readback_valid else None,
            "not_seen_sequence_sample": sorted(missing)[:30] if readback_valid else None,
            "capture_return_to_hdmi_in_latency": stats_summary(rx["latency_from_capture_ms"]),
            "hdmi_submit_to_hdmi_in_latency": stats_summary(rx["latency_from_submit_ms"]),
            "latency_sample_scope": "only sequence-tagged unique frames observed at HDMI IN",
            "loopback_frame_shape": rx["first_frame_shape"],
            "loopback_frame_dtype": rx["first_frame_dtype"],
            "sample_tagged_frame_stddev": None,
            "sample_tagged_frame_sha256": None,
            "reader_error": rx["reader_error"],
        }
        result["sequence_marker"] = {
            "camera_frames_seen_by_app": capture_stats["frames"],
            "application_overwrites_before_hdmi": capture_stats["overwrites"],
            "marker_sequence_bits": 12,
            "marker_recognized": len(rx["unique_sequences"]) > 0,
            "sent_frame_tracking_note": "V4L2/OpenCV does not expose UVC kernel sequence gaps here; app queue overwrites and frames absent from HDMI IN are counted separately",
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        trace(f"{fourcc}: exception before cleanup: {type(exc).__name__}: {exc}")
    finally:
        trace(f"{fourcc}: cleanup begins")
        stop_capture.set()
        if capture_thread is not None:
            capture_thread.join(timeout=2.0)
        trace(f"{fourcc}: camera thread alive={capture_thread.is_alive() if capture_thread else False}")
        trace(f"{fourcc}: releasing UVC camera")
        cap.release()
        trace(f"{fourcc}: UVC camera released")
        if result.get("error"):
            # PYNQ's DMA stop methods spin until hardware reports idle. If
            # initialization failed, that status may never arrive; avoid
            # hanging cleanup and let process teardown release userspace state.
            result["hdmi_cleanup_skipped_after_error"] = True
            trace(f"{fourcc}: skipping HDMI close after initialization error")
        elif in_started:
            try:
                trace(f"{fourcc}: closing HDMI IN")
                hdmi_in.close()
                trace(f"{fourcc}: HDMI IN closed")
            except Exception as exc:
                result["hdmi_in_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if out_started and not result.get("error"):
            try:
                trace(f"{fourcc}: closing HDMI OUT")
                hdmi_out.close()
                trace(f"{fourcc}: HDMI OUT closed")
            except Exception as exc:
                result["hdmi_out_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if input_loop is not None:
            input_loop.close()
            asyncio.set_event_loop(None)
        trace(f"{fourcc}: cleanup finished")
    return result


def main():
    faulthandler.enable()
    trace("process started")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15.0,
                        help="seconds per C920 format (default: 15)")
    parser.add_argument("--formats", nargs=1, choices=("MJPG", "YUYV"),
                        default=("MJPG",),
                        help="one C920 format per process; run separately for each format")
    parser.add_argument("--save-previews", action="store_true",
                        help="save one frame recovered by HDMI IN per tested format")
    parser.add_argument("--preview-dir", default="/home/xilinx/phase6_previews",
                        help="directory for optional HDMI-IN previews")
    args = parser.parse_args()
    if args.duration < 3:
        parser.error("--duration must be at least 3 seconds")

    report = {
        "board": "PYNQ-Z2",
        "opencv_version": cv2.__version__,
        "duration_per_mode_s": args.duration,
        "camera_modes_tested": [],
        "overlay_downloaded_this_run": False,
        "pcm_overlay_loaded": False,
        "camera_frames_saved": bool(args.save_previews),
    }
    try:
        report["base_artifacts"] = verify_base_artifacts()
        trace("base artifacts verified")
        use_legacy_metadata_parser()
        overlay = Overlay(str(BASE_BIT), download=True, device=Device.active_device)
        trace("official base overlay downloaded")
        report["overlay_downloaded_this_run"] = True
        report["base_load_proof"] = verify_loaded_base()
        trace("base load proof verified")
        if not overlay.is_loaded():
            raise RuntimeError("Official base overlay did not report as loaded")
        report["metadata_parser"] = type(overlay.parser).__name__
        report["parsed_ip_count"] = len(overlay.ip_dict)
        report["parsed_hierarchy_count"] = len(overlay.hierarchy_dict)
        video = overlay.video
        for fourcc in args.formats:
            preview_path = (Path(args.preview_dir) / f"phase6_{fourcc}_hdmi_in.jpg"
                            if args.save_previews else None)
            report["camera_modes_tested"].append(
                run_mode(fourcc, args.duration, video, preview_path)
            )
        report["base_load_proof_after"] = verify_loaded_base()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        trace(f"top-level exception: {type(exc).__name__}: {exc}")
    print(json.dumps(report, indent=2, sort_keys=True))
    if report.get("error") or not report.get("camera_modes_tested"):
        raise SystemExit(1)
    if any(
        "error" in mode
        or ("metrics" in mode and not mode["metrics"].get("hdmi_readback_measurements_valid"))
        for mode in report["camera_modes_tested"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
