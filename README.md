# PYNQ acoustic camera — software

Software workspace for the acoustic camera on **PYNQ-Z2**.
Distribution: `pynq-acoustic-camera`; import: `pynq_acoustic_camera`.

Current milestone: distribute the reviewed **two-channel XADC reference** from
[HW v0.1.0-rc.2](https://github.com/SiririComun/hw-acoustic-camera/releases/tag/v0.1.0-rc.2).
Four-channel PDM, source localization, webcam and HDMI integration are future work.

The loader pins the release and manifest hash, validates every cached BIT/HWH
pair, and programs FPGA only when explicitly requested. Package installation and
import do not program hardware. Notebook installation preserves existing files.

See [distribution and API](docs/SOFTWARE_DISTRIBUTION.md) for the branch reinstall
command (`--force-reinstall --no-cache-dir --no-deps`), cache layout and limits.
Python >=3.9 is required. Packaging/loader behavior was implemented in 1.6; the baseline notebook and local
distribution verification are prepared in 1.7. The initial isolated wheel check
passed 16 cases on Python 3.13. Board integration remains for 1.8.
See [local verification](docs/VERIFYING_DISTRIBUTION.md). No board validation has been performed.

The original analytics and XADC routines are retained under the new namespace.
[Original README](docs/LEGACY_SOUND_LOCALIZER.md), old test names and historical
context describe the source project and do not establish acoustic-camera results.
The legacy driver is explicitly opted into and is not a four-microphone driver.
