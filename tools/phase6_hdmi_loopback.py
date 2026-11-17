#!/usr/bin/env python3
"""Send a deterministic pattern through the PYNQ-Z2 HDMI loopback.

This phase-6.2 probe explicitly downloads the verified official PYNQ-Z2
base.bit through PYNQ, then verifies that it is the FPGA-manager firmware in
use. Downloading it reconfigures the PL and replaces any active overlay. The
probe uses PYNQ's bundled legacy HWH parser as an in-process fallback for the
modern metadata parser, which fails on this board image.

Run as root on the board with HDMI OUT connected to HDMI IN.
"""

import hashlib
import json
import re
import subprocess
import time
import traceback
from pathlib import Path

import numpy as np
import pynq
import pynq.lib.video  # Register video drivers before constructing Overlay.
from pynq import Overlay
from pynq.lib.video import VideoMode
from pynq.pl_server.device import Device
from pynq.pl_server.embedded_device import (
    EmbeddedDevice,
    _create_xclbin,
    _unify_dictionaries,
    bit2bin,
)
from pynq.pl_server.hwh_parser import HWH
from pynq.pl_server.xclbin_parser import XclBin


BASE_BIT = Path(pynq.__file__).parent / "overlays/base/base.bit"
BASE_BIN = Path("/lib/firmware/base.bin")
STATE_FILE = Path(pynq.__file__).parent / "pl_server/global_pl_state_.json"
FPGA_STATE = Path("/sys/class/fpga_manager/fpga0/state")
FPGA_FLAGS = Path("/sys/class/fpga_manager/fpga0/flags")
BAR_VALUES = [16, 48, 80, 112, 144, 176, 208, 240]
EXPECTED_BASE_BIT_SHA256 = "3239e9cc37ee4993c25f02ae6c97da0b3372f0e722e267d10073e20adc5d6717"
EXPECTED_BASE_HWH_SHA256 = "222898ef2c0ccc4a0c47608e99d8dc7c9c6cdfed5130964884ff35005827ef3a"


def verify_base_artifacts():
    """Require the board's installed official base bitstream and handoff file."""
    bit_bytes = BASE_BIT.read_bytes()
    hwh_bytes = BASE_BIT.with_suffix(".hwh").read_bytes()
    bit_hash = hashlib.sha256(bit_bytes).hexdigest()
    hwh_hash = hashlib.sha256(hwh_bytes).hexdigest()
    if bit_hash != EXPECTED_BASE_BIT_SHA256 or hwh_hash != EXPECTED_BASE_HWH_SHA256:
        raise RuntimeError(
            "Installed base.bit/base.hwh do not match the verified PYNQ-Z2 image"
        )
    return {"base_bit_sha256": bit_hash, "base_hwh_sha256": hwh_hash}


def verify_loaded_base():
    """Require independent evidence that PYNQ has just loaded base.bin."""
    manager_state = FPGA_STATE.read_text(encoding="ascii").strip()
    manager_flags = FPGA_FLAGS.read_text(encoding="ascii").strip()
    if manager_state != "operating" or manager_flags not in ("0", "0x0"):
        raise RuntimeError(
            "FPGA manager is not in the expected operating/flags=0 state: "
            f"state={manager_state!r}, flags={manager_flags!r}"
        )

    bit_bytes = BASE_BIT.read_bytes()
    bin_bytes = BASE_BIN.read_bytes()
    expected_bin = bit2bin(bit_bytes)
    if expected_bin != bin_bytes:
        raise RuntimeError("/lib/firmware/base.bin does not match the official base.bit")

    log = subprocess.run(
        ["dmesg"], check=True, capture_output=True, text=True
    ).stdout
    loads = re.findall(
        r"fpga_manager fpga0: writing (\S+) to Xilinx Zynq FPGA Manager", log
    )
    if not loads or Path(loads[-1]).name != BASE_BIN.name:
        latest = loads[-1] if loads else "no FPGA-manager load entry found"
        raise RuntimeError(f"Latest FPGA-manager load is not base.bin: {latest}")

    if not STATE_FILE.exists():
        raise RuntimeError("PYNQ did not write its global PL state after downloading base.bit")
    global_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state_bit = Path(global_state.get("bitfile_name", "")).resolve()
    state_hash = global_state.get("bitfile_hash")
    if state_bit != BASE_BIT.resolve() or state_hash != hashlib.sha1(bit_bytes).hexdigest():
        raise RuntimeError("PYNQ global state does not identify the verified base.bit")

    return {
        "manager_state": manager_state,
        "manager_flags": manager_flags,
        "latest_fpga_manager_load": loads[-1],
        "base_bit_sha256": hashlib.sha256(bit_bytes).hexdigest(),
        "base_bin_sha256": hashlib.sha256(bin_bytes).hexdigest(),
        "base_bin_matches_bit2bin": True,
        "pynq_global_state_present": True,
    }


def use_legacy_metadata_parser():
    """Replace only this process's parser method; installed PYNQ files stay intact."""

    def parse_hwh(self, bitfile_name, partial=False):
        parser = HWH(hwh_name=str(Path(bitfile_name).with_suffix(".hwh")))
        # Build the same synthetic XRT memory topology PYNQ uses for HWH-only
        # overlays. PYNQ's legacy parser supplies the base HWH dictionaries;
        # XRT needs the synthetic PS DDR entry before VDMA can allocate frames.
        parser.xclbin_data = _create_xclbin(parser.mem_dict)
        xclbin_parser = XclBin(xclbin_data=parser.xclbin_data)
        _unify_dictionaries(parser, xclbin_parser)
        parser.bin_data = bit2bin(Path(bitfile_name).read_bytes())
        parser.dtbo_data = None
        # PYNQ 3.1's Overlay expects this newer attribute when copying metadata.
        parser.systemgraph = None
        if "PSDDR" not in parser.mem_dict or parser.mem_dict["PSDDR"].get("base_address") != 0:
            raise RuntimeError("Synthetic XRT metadata did not expose PS DDR")
        return parser

    EmbeddedDevice.get_bitfile_metadata = parse_hwh


def gray_bar_means(frame):
    image = np.asarray(frame)
    h, w = image.shape[:2]
    y0, y1 = h // 2 - 12, h // 2 + 12
    means = []
    for i in range(8):
        x = int((i + 0.5) * w / 8)
        patch = image[y0:y1, max(0, x - 12):min(w, x + 12)]
        means.append(round(float(patch.mean()), 2))
    return image, means


result = {
    "board": "PYNQ-Z2",
    "base_bit": str(BASE_BIT),
    "requested_mode": {"width": 640, "height": 480, "bits_per_pixel": 24, "fps": 60},
    "expected_gray_bars": BAR_VALUES,
    "overlay_downloaded": False,
}
hdmi_in = hdmi_out = None
input_configured = output_configured = False
exit_status = 0

try:
    result["base_artifacts"] = verify_base_artifacts()
    use_legacy_metadata_parser()

    device = Device.active_device
    overlay = Overlay(str(BASE_BIT), download=True, device=device)
    result["overlay_downloaded"] = True
    result["base_load_proof"] = verify_loaded_base()

    result["metadata_parser"] = type(overlay.parser).__name__
    result["parsed_ip_count"] = len(overlay.ip_dict)
    result["parsed_hierarchy_count"] = len(overlay.hierarchy_dict)
    result["overlay_is_loaded"] = bool(overlay.is_loaded())
    if not result["overlay_is_loaded"]:
        raise RuntimeError("Could not associate the verified base image with the overlay")

    video = overlay.video
    hdmi_in, hdmi_out = video.hdmi_in, video.hdmi_out
    hdmi_in.cacheable_frames = False
    hdmi_out.cacheable_frames = False

    mode = VideoMode(640, 480, 24, 60)
    output_configured = True
    hdmi_out.configure(mode)
    hdmi_out.start()

    pattern = np.zeros((mode.height, mode.width, 3), dtype=np.uint8)
    for i, value in enumerate(BAR_VALUES):
        x0, x1 = i * mode.width // 8, (i + 1) * mode.width // 8
        pattern[:, x0:x1, :] = value
    # Asymmetric top marker makes rotation/cropping easier to spot in captures.
    pattern[32:64, 32:64, :] = 240
    pattern[32:64, 64:96, :] = 16
    pattern[32:64, 96:128, :] = 240
    pattern[32:64, 128:160, :] = 16

    for _ in range(3):
        out_frame = hdmi_out.newframe()
        np.asarray(out_frame)[...] = pattern
        hdmi_out.writeframe(out_frame)
    time.sleep(0.5)

    input_configured = True
    hdmi_in.configure()
    input_mode = hdmi_in.mode
    result["input_mode"] = {
        "width": input_mode.width,
        "height": input_mode.height,
        "bits_per_pixel": input_mode.bits_per_pixel,
    }
    hdmi_in.start()

    received = []
    for _ in range(5):
        in_frame = hdmi_in.readframe()
        try:
            image, means = gray_bar_means(in_frame)
            received.append({
                "shape": list(image.shape),
                "dtype": str(image.dtype),
                "gray_bar_means": means,
                "stddev": round(float(image.std()), 3),
                "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "bars_monotonic": all(a < b for a, b in zip(means, means[1:])),
                "bars_within_tolerance": all(
                    abs(actual - expected) <= 40
                    for actual, expected in zip(means, BAR_VALUES)
                ),
            })
        finally:
            in_frame.freebuffer()
    result["frames_read"] = len(received)
    result["received_frames"] = received
    result["pattern_check_passed"] = all(
        row["bars_monotonic"] and row["bars_within_tolerance"] for row in received
    )
except Exception as exc:
    exit_status = 1
    result["error"] = "{}: {}".format(type(exc).__name__, exc)
    result["traceback"] = traceback.format_exc()
finally:
    if input_configured and hdmi_in is not None:
        try:
            hdmi_in.close()
        except Exception as exc:
            result["input_cleanup_error"] = "{}: {}".format(type(exc).__name__, exc)
    if output_configured and hdmi_out is not None:
        try:
            hdmi_out.close()
        except Exception as exc:
            result["output_cleanup_error"] = "{}: {}".format(type(exc).__name__, exc)
    try:
        result["base_load_proof_after"] = verify_loaded_base()
    except Exception as exc:
        result["base_state_after_error"] = "{}: {}".format(type(exc).__name__, exc)

print(json.dumps(result, indent=2, sort_keys=True))
if exit_status:
    raise SystemExit(exit_status)
