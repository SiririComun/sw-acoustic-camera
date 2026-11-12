#!/usr/bin/env python3
"""Independent digital RAW vectors and XSim trace checks; no PYNQ dependency.

This tool checks the sampler before FIFO/DMA integration. It does not certify
sensor electrical timing or acoustic behavior. No board is accessed.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path


MASK = (1 << 32) - 1
SEEDS = (0x00000001, 0x12345678, 0xDEADBEEF, 0xC001D00D)
SUITES = {
    "unit": [(1, "physical", 4096), (2, "pattern", 1), (3, "pattern", 2),
             (4, "pattern", 17), (5, "pattern", 4096), (6, "pattern", 65536),
             (7, "pattern", 17), (8, "physical", 257), (9, "pattern", 2),
             (10, "physical", 1), (11, "pattern", 1)],
    "clock": [(101, "physical", 257), (102, "pattern", 17),
              (103, "physical", 17), (104, "pattern", 2)],
}


def physical_words(n_frames):
    """Four distinct deterministic bit streams, independent of RTL internals."""
    states = list(SEEDS)
    words = []
    for _ in range(n_frames):
        words.append(sum((value & 1) << channel for channel, value in enumerate(states)))
        for channel, value in enumerate(states):
            value ^= (value << 13) & MASK
            value ^= value >> 17
            value ^= (value << 5) & MASK
            states[channel] = value & MASK
    return words


def generate(directory):
    directory.mkdir(parents=True, exist_ok=False)
    words = physical_words(4096)
    path = directory / "physical_words.hex"
    path.write_text("".join("{:08X}\n".format(word) for word in words))
    spec = {
        "scope": "digital_sampling_only_no_sensor_model",
        "seeds": list(SEEDS),
        "physical_frames": len(words),
        "physical_vectors_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "suites": {name: [{"case_id": case, "source": source, "n_frames": n}
                           for case, source, n in cases] for name, cases in SUITES.items()},
    }
    (directory / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")


def verify(directory, trace, suite, phase_trace=None):
    spec = json.loads((directory / "spec.json").read_text())
    vector_path = directory / "physical_words.hex"
    if hashlib.sha256(vector_path.read_bytes()).hexdigest() != spec["physical_vectors_sha256"]:
        raise ValueError("Stimulus file hash mismatch")
    golden = physical_words(spec["physical_frames"])
    actual_vectors = [int(line, 16) for line in vector_path.read_text().splitlines()]
    if actual_vectors != golden:
        raise ValueError("Stimulus does not match independent channel generators")
    expected_cases = SUITES[suite]
    results = []
    with trace.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["case_id", "index", "word", "sample_cycle", "enqueue_cycle"]:
            raise ValueError("Unexpected trace columns")
        for case_id, source, n_frames in expected_cases:
            previous_sample = None
            for index in range(n_frames):
                row = next(reader, None)
                if row is None:
                    raise ValueError("Missing frame in case {} at {}".format(case_id, index))
                row = {key: int(value) for key, value in row.items()}
                expected = index if source == "pattern" else golden[index]
                if (row["case_id"], row["index"], row["word"]) != (case_id, index, expected):
                    raise ValueError("Data/order mismatch in case {} frame {}: {}".format(case_id, index, row))
                if row["enqueue_cycle"] - row["sample_cycle"] != 1:
                    raise ValueError("Unequal or incorrect capture-to-enqueue latency")
                if previous_sample is not None and row["sample_cycle"] - previous_sample != 10:
                    raise ValueError("Missing/duplicated sample event")
                if source == "physical" and row["word"] & ~0xF:
                    raise ValueError("Nonzero reserved physical RAW bits")
                previous_sample = row["sample_cycle"]
            results.append({"case_id": case_id, "source": source, "frames_checked": n_frames,
                            "word_equality": "pass", "cadence_base_cycles": 10,
                            "enqueue_latency_base_cycles": 1})
        if next(reader, None) is not None:
            raise ValueError("Unexpected extra frames in trace")
    channel_statistics = []
    channel_sequences = []
    for channel in range(4):
        bits = [(word >> channel) & 1 for word in golden]
        if len(set(bits)) != 2:
            raise ValueError("Physical stimulus channel is constant")
        channel_sequences.append(tuple(bits))
        channel_statistics.append({"channel": channel, "zeros": bits.count(0),
                                   "ones": bits.count(1),
                                   "transitions": sum(a != b for a, b in zip(bits, bits[1:]))})
    if len(set(channel_sequences)) != 4:
        raise ValueError("Physical stimulus channels must be distinct")
    phase_result = None
    if suite == "clock":
        if phase_trace is None:
            raise ValueError("Real clock suite requires a phase trace")
        previous_sample = None
        phase_count = 0
        with phase_trace.open(newline="") as handle:
            for row in csv.DictReader(handle):
                sample, fall, offset = (int(row[key]) for key in ("sample_cycle", "fall_cycle", "offset_ps"))
                if sample - fall != 3 or abs(offset - 125_000) > 10:
                    raise ValueError("Incorrect real-clock capture phase")
                if previous_sample is not None and sample - previous_sample != 10:
                    raise ValueError("Incorrect real-clock capture cadence")
                previous_sample = sample
                phase_count += 1
        if phase_count < sum(n for _, _, n in expected_cases):
            raise ValueError("Insufficient real-clock phase samples")
        phase_result = {"result": "pass", "events_checked": phase_count,
                        "sample_cycles_after_pdm_fall": 3,
                        "nominal_offset_ns": 125, "offset_tolerance_ps": 10,
                        "phase_trace_sha256": hashlib.sha256(phase_trace.read_bytes()).hexdigest()}
    return {"result": "pass", "scope": "digital_sampler_simulation_only", "suite": suite,
            "cases": results, "frames_checked": sum(n for _, _, n in expected_cases),
            "stimulus_channel_statistics": channel_statistics,
            "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "real_clock_phase": phase_result,
            "fifo_dma_board_acceptance": "not_run"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("generate")
    command.add_argument("--directory", type=Path, required=True)
    command = commands.add_parser("verify")
    command.add_argument("--directory", type=Path, required=True)
    command.add_argument("--trace", type=Path, required=True)
    command.add_argument("--suite", choices=SUITES, required=True)
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--phase-trace", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.directory)
    else:
        result = verify(args.directory, args.trace, args.suite, args.phase_trace)
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        print("{}: {} cases / {} frames passed".format(args.suite, len(result["cases"]), result["frames_checked"]))


if __name__ == "__main__":
    main()
