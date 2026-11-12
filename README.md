# PYNQ acoustic camera — software

Software workspace for the acoustic camera on **PYNQ-Z2**.
Distribution: `pynq-acoustic-camera`; import: `pynq_acoustic_camera`.

The accepted hardware remains the reviewed **two-channel XADC reference** from
[HW v0.1.0-rc.2](https://github.com/SiririComun/hw-acoustic-camera/releases/tag/v0.1.0-rc.2).
Phase 2 was functionally accepted on PYNQ-Z2 on 2026-11-11 (America/Bogota), on `phase/02-clock-control`: PDM identity and clock
control are implemented. The first board attempt was interrupted; its candidate
was retired because its auxiliary reset was active at the tied-low input. The
replacement explicitly configures the inactive reset polarity and uses a new
BUILD_ID. Unsupported commands report ERROR_FLAGS with AXI OKAY so software can
inspect and clear them. The replacement passed identity checks, ten clock cycles, START/CLEAR and
reset by a second load. The clock was disabled at the end; microphones stayed
disconnected. External waveform measurements remain unverified.
Accepted wheel source: `397b098a2f4cd40eda072808804fa97fc952221d`.
Four-channel capture remains phase 3.


The loader pins the release and manifest hash, validates every cached BIT/HWH
pair, and programs FPGA only when explicitly requested. Package installation and
import do not program hardware. Notebook installation preserves existing files.
The separate `PdmCandidateLoader` verifies the reviewed local candidate hashes,
HWH mapping, inactive reset topology and BUILD_ID; `program_overlay()` requires an explicit board
confirmation. It does not change the XADC release loader.

See [distribution and API](docs/SOFTWARE_DISTRIBUTION.md) for the branch reinstall
command (`--force-reinstall --no-cache-dir --no-deps`), cache layout and limits.
Python >=3.9 is required. Packaging/loader behavior was implemented in 1.6; the baseline notebook and local
distribution verification are prepared in 1.7. The initial isolated wheel check
passed 16 cases on Python 3.13. The phase-1 XADC integration has been accepted.
See [local verification](docs/VERIFYING_DISTRIBUTION.md). Recorded PDM board evidence and the Vivado PDF are under `evidence/phase-02-closure/`
in the common workspace, with the acceptance lock in `context/integration-lock.json`.

The original analytics and XADC routines are retained under the new namespace.
[Original README](docs/LEGACY_SOUND_LOCALIZER.md), old test names and historical
context describe the source project and do not establish acoustic-camera results.
The legacy driver is explicitly opted into and is not a four-microphone driver.
