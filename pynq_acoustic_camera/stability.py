"""Serial PCM acquisition stability campaign for phase 5.

The campaign makes exactly 100 microphone captures of 48,000 frames each.
It stores each returned block and its validation record before proceeding,
stops on the first failure, and never retries a capture. Host call intervals
describe pauses between API calls; they are not acoustic sample timestamps and
do not establish continuous acquisition between blocks.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import statistics
import time
import uuid

import numpy as np

from . import __version__ as PACKAGE_VERSION


BLOCK_COUNT = 100
BLOCK_FRAMES = 48000
CHANNEL_COUNT = 4
SAMPLE_RATE_HZ = 48000
SAMPLE_DTYPE = "int32"
BLOCK_BYTES = BLOCK_FRAMES * CHANNEL_COUNT * 4
MIN_FREE_BYTES = BLOCK_COUNT * BLOCK_BYTES + 16 * 1024 * 1024
EXPECTED_BUILD_ID = "B691CC9C9F6249AD938D2063E4767C54"
EXPECTED_BIT_SHA256 = "5b7b14e34d9372302132746d9ad24475c4df9a1c66f06236ace752b439e91cf2"
EXPECTED_HWH_SHA256 = "fc757a78ae638b88eea46fa9a75a0fa67ecdaa09c2599aec19732102bdc9824f"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError("Not JSON serializable: {}".format(type(value).__name__))


def _write_json(path, value):
    """Replace a JSON file atomically so a process interruption leaves valid evidence."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False,
                  default=_json_default)
        stream.write("\n")
        stream.flush()
    temporary.replace(path)


def _write_samples(path, samples):
    temporary = Path(path).with_name(Path(path).name + ".tmp")
    with temporary.open("wb") as stream:
        np.save(stream, samples, allow_pickle=False)
        stream.flush()
    temporary.replace(path)


def _check_equal(errors, label, actual, expected):
    if actual != expected:
        errors.append("{}: expected {!r}, got {!r}".format(label, expected, actual))


def _validate_result(result):
    """Return validation findings for one completed capture; do not modify data."""
    errors = []
    samples = getattr(result, "samples", None)
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        return ["capture metadata is missing or is not a dictionary"]
    if samples is None:
        return ["capture samples are missing"]

    _check_equal(errors, "format", metadata.get("format"), "pcm_s24_in_s32")
    _check_equal(errors, "source", metadata.get("source"), "microphones")
    _check_equal(errors, "channels", metadata.get("channels"), CHANNEL_COUNT)
    _check_equal(errors, "n_frames", metadata.get("n_frames"), BLOCK_FRAMES)
    _check_equal(errors, "nominal_sample_rate_hz", metadata.get("nominal_sample_rate_hz"),
                 SAMPLE_RATE_HZ)
    _check_equal(errors, "dtype metadata", metadata.get("dtype"), SAMPLE_DTYPE)
    _check_equal(errors, "shape metadata", metadata.get("shape"),
                 [BLOCK_FRAMES, CHANNEL_COUNT])
    _check_equal(errors, "bytes metadata", metadata.get("bytes"), BLOCK_BYTES)
    _check_equal(errors, "build_id", str(metadata.get("build_id", "")).upper(),
                 EXPECTED_BUILD_ID)
    _check_equal(errors, "filter_applied", metadata.get("filter_applied"), True)
    _check_equal(errors, "filter_ready_at_start", metadata.get("filter_ready_at_start"), True)
    _check_equal(errors, "filter_reset_per_block", metadata.get("filter_reset_per_block"), False)

    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("candidate provenance is missing")
    else:
        _check_equal(errors, "BIT SHA-256", str(provenance.get("bit_sha256", "")).lower(),
                     EXPECTED_BIT_SHA256)
        _check_equal(errors, "HWH SHA-256", str(provenance.get("hwh_sha256", "")).lower(),
                     EXPECTED_HWH_SHA256)

    try:
        shape = tuple(samples.shape)
        _check_equal(errors, "sample array shape", shape, (BLOCK_FRAMES, CHANNEL_COUNT))
        _check_equal(errors, "sample array dtype", np.dtype(samples.dtype).name, SAMPLE_DTYPE)
        _check_equal(errors, "sample array bytes", int(samples.nbytes), BLOCK_BYTES)
        if np.any(samples < -8388608) or np.any(samples > 8388607):
            errors.append("sample array contains values outside signed-24 range")
    except Exception as error:
        errors.append("sample array inspection failed: {}: {}".format(
            type(error).__name__, error))

    counters = metadata.get("counters")
    if not isinstance(counters, dict):
        errors.append("hardware counters are missing")
    else:
        for key, expected in (("frames", BLOCK_FRAMES), ("words", BLOCK_FRAMES * CHANNEL_COUNT),
                              ("dma_bytes", BLOCK_BYTES), ("overflows", 0), ("clips", 0),
                              ("error_flags", 0)):
            _check_equal(errors, "counter {}".format(key), counters.get(key), expected)
        status = counters.get("status")
        if not isinstance(status, (int, np.integer)):
            errors.append("capture status register is missing")
        else:
            status = int(status)
            if not status & 0x04:
                errors.append("capture DONE status bit is clear")
            if status & 0x02:
                errors.append("capture BUSY status bit remains set")
            if status & 0x08:
                errors.append("capture ERROR status bit is set")
            if not status & 0x10:
                errors.append("PCM FILTER_READY status bit is clear")
        dma_status = counters.get("dma_status")
        if not isinstance(dma_status, (int, np.integer)):
            errors.append("DMA status register is missing")
        else:
            dma_status = int(dma_status)
            if not dma_status & 0x02:
                errors.append("DMA IDLE status bit is clear")
            if dma_status & 0x70:
                errors.append("DMA error status bits are set: 0x{:02X}".format(dma_status & 0x70))

    return errors


def _duration_summary(values):
    if not values:
        return {"count": 0, "min_seconds": None, "median_seconds": None,
                "p95_nearest_rank_seconds": None, "max_seconds": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {"count": len(ordered), "min_seconds": ordered[0],
            "median_seconds": statistics.median(ordered),
            "p95_nearest_rank_seconds": ordered[p95_index],
            "max_seconds": ordered[-1]}


def _check_candidate(array):
    identity = getattr(getattr(array, "clock", None), "identity", None)
    build_id = str(getattr(identity, "build_id", "")).upper()
    if build_id != EXPECTED_BUILD_ID:
        raise ValueError("Loaded BUILD_ID does not match the approved PCM candidate")
    if getattr(array, "pcm_rate_hz", None) != SAMPLE_RATE_HZ:
        raise ValueError("Loaded interface does not report 48 kHz PCM")
    provenance = getattr(array, "_provenance", None)
    if not isinstance(provenance, dict):
        raise ValueError("Array was not attached with candidate hash provenance")
    if str(provenance.get("bit_sha256", "")).lower() != EXPECTED_BIT_SHA256:
        raise ValueError("Attached BIT hash does not match the approved PCM candidate")
    if str(provenance.get("hwh_sha256", "")).lower() != EXPECTED_HWH_SHA256:
        raise ValueError("Attached HWH hash does not match the approved PCM candidate")


def run_pcm_stability_campaign(array, evidence_root, *, operator_context=None):
    """Run and persist one serial 100-block PCM microphone campaign.

    The clock and PCM filter are enabled and allowed to become ready once before
    block 1. A failure stops the campaign immediately. The function does not
    retry, inject errors, or perform abort/reset/timeout recovery exercises.

    Returns a report dictionary with ``status`` equal to ``pass`` or ``fail``.
    A failing report remains on disk with all blocks attempted up to the first
    failure. KeyboardInterrupt/SystemExit are recorded and then re-raised.
    """
    _check_candidate(array)
    if operator_context is not None:
        # Ensure the context can be persisted before enabling the hardware.
        json.dumps(operator_context, allow_nan=False, default=_json_default)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(evidence_root).free
    if free_bytes < MIN_FREE_BYTES:
        raise OSError("Evidence storage needs at least {} free bytes; found {}".format(
            MIN_FREE_BYTES, free_bytes))
    campaign_dir = Path(evidence_root) / (
        "pcm-stability-{}-{}".format(stamp, uuid.uuid4().hex[:8]))
    campaign_dir.mkdir(parents=True, exist_ok=False)
    report_path = campaign_dir / "campaign-report.json"
    report = {
        "schema_version": 1,
        "status": "running",
        "campaign_id": campaign_dir.name,
        "scope": "serial_physical_pcm_block_stability",
        "software_version": PACKAGE_VERSION,
        "continuous_acquisition_claimed": False,
        "capture_count_required": BLOCK_COUNT,
        "capture_count_passed": 0,
        "block_frames": BLOCK_FRAMES,
        "channels": CHANNEL_COUNT,
        "sample_rate_hz_nominal": SAMPLE_RATE_HZ,
        "expected_dtype": SAMPLE_DTYPE,
        "expected_bytes_per_block": BLOCK_BYTES,
        "expected_sample_payload_bytes": BLOCK_COUNT * BLOCK_BYTES,
        "minimum_free_storage_bytes": MIN_FREE_BYTES,
        "free_storage_bytes_at_start": free_bytes,
        "candidate": {"build_id": EXPECTED_BUILD_ID,
                      "bit_sha256": EXPECTED_BIT_SHA256,
                      "hwh_sha256": EXPECTED_HWH_SHA256},
        "operator_context": operator_context or {},
        "timing_semantics": {
            "clock_and_filter_readiness_excluded_from_block_timings": True,
            "host_call_times_are_not_acoustic_sample_timestamps": True,
            "finish_to_next_start_is_an_inter_call_pause_not_a_within_block_loss": True,
            "no_continuity_between_api_calls_is_claimed": True,
        },
        "retry_policy": "stop_on_first_failure; no retries",
        "blocks": [],
        "timing_summary": {"call_duration": _duration_summary([]),
                           "finish_to_next_start_pause": _duration_summary([])},
        "started_utc": _utc_now(),
    }
    _write_json(report_path, report)

    previous_end_ns = None
    current_record = None
    fatal_exception = None
    clock_enable_attempted = False
    durations = []
    pauses = []
    try:
        readiness_start_ns = time.monotonic_ns()
        report["readiness_start_utc"] = _utc_now()
        clock_enable_attempted = True
        array.enable_clock()
        array.wait_filter_ready()
        report["readiness_end_utc"] = _utc_now()
        report["readiness_duration_seconds"] = (
            time.monotonic_ns() - readiness_start_ns) / 1_000_000_000
        _write_json(report_path, report)

        for block_index in range(1, BLOCK_COUNT + 1):
            stem = "block-{:03d}".format(block_index)
            record = {"index": block_index, "status": "running",
                      "samples_file": stem + ".npy",
                      "metadata_file": stem + ".json",
                      "host_call_start_utc": _utc_now(),
                      "host_call_start_monotonic_ns": time.monotonic_ns(),
                      "finish_to_next_start_pause_seconds": None}
            current_record = record
            if previous_end_ns is not None:
                pause = (record["host_call_start_monotonic_ns"] - previous_end_ns) / 1_000_000_000
                record["finish_to_next_start_pause_seconds"] = pause
                pauses.append(pause)
            report["blocks"].append(record)
            _write_json(report_path, report)

            try:
                result = array.capture(BLOCK_FRAMES, format="pcm_s24_in_s32",
                                       source="microphones")
            except BaseException as error:
                end_ns = time.monotonic_ns()
                record.update(status="capture_error",
                               host_call_end_utc=_utc_now(),
                              host_call_end_monotonic_ns=end_ns,
                               error_type=type(error).__name__, error=str(error))
                record["call_duration_seconds"] = (
                    end_ns - record["host_call_start_monotonic_ns"]) / 1_000_000_000
                durations.append(record["call_duration_seconds"])
                previous_end_ns = end_ns
                diagnostics = getattr(error, "diagnostics", None)
                if diagnostics is not None:
                    record["diagnostics"] = diagnostics
                _write_json(campaign_dir / record["metadata_file"],
                            {"block": record, "capture_metadata": None,
                             "validation_errors": ["capture call did not return a block"]})
                report.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit))
                              else "fail", failed_block=block_index)
                fatal_exception = error if isinstance(error, (KeyboardInterrupt, SystemExit)) else None
                break

            end_ns = time.monotonic_ns()
            record["host_call_end_utc"] = _utc_now()
            record["host_call_end_monotonic_ns"] = end_ns
            duration = (end_ns - record["host_call_start_monotonic_ns"]) / 1_000_000_000
            record["call_duration_seconds"] = duration
            durations.append(duration)
            previous_end_ns = end_ns

            samples = getattr(result, "samples", None)
            metadata = getattr(result, "metadata", None)
            if samples is not None:
                _write_samples(campaign_dir / record["samples_file"], samples)
            validation_errors = _validate_result(result)
            record["validation_errors"] = validation_errors
            record["status"] = "pass" if not validation_errors else "validation_error"
            _write_json(campaign_dir / record["metadata_file"],
                        {"block": record, "capture_metadata": metadata,
                         "validation_errors": validation_errors})
            if validation_errors:
                report.update(status="fail", failed_block=block_index)
                _write_json(report_path, report)
                break
            report["capture_count_passed"] += 1
            _write_json(report_path, report)
            current_record = None
    except BaseException as error:
        if current_record is not None and current_record.get("status") == "running":
            current_record.update(status="campaign_error", error_type=type(error).__name__,
                                  error=str(error))
        report.setdefault("startup_or_campaign_error", {
            "type": type(error).__name__, "message": str(error)})
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            report["status"] = "interrupted"
            fatal_exception = error
        else:
            report["status"] = "fail"
    finally:
        if clock_enable_attempted:
            try:
                array.disable_clock()
                report["clock_disabled"] = True
            except Exception as error:
                report["clock_disabled"] = False
                report["clock_cleanup_error"] = "{}: {}".format(type(error).__name__, error)
                report["status"] = "fail"
        report["timing_summary"] = {
            "call_duration": _duration_summary(durations),
            "finish_to_next_start_pause": _duration_summary(pauses),
        }
        report["capture_count_passed"] = sum(
            1 for block in report["blocks"] if block.get("status") == "pass")
        if report.get("status") == "running":
            report["status"] = ("pass" if report["capture_count_passed"] == BLOCK_COUNT
                                and report.get("clock_disabled") is True else "fail")
        if report["status"] == "pass" and report["capture_count_passed"] != BLOCK_COUNT:
            report["status"] = "fail"
        report["finished_utc"] = _utc_now()
        report["campaign_report_file"] = str(report_path)
        _write_json(report_path, report)

    if fatal_exception is not None:
        raise fatal_exception
    return report
