# Phase 1 software distribution

## Identity and current scope

Distribution: `pynq-acoustic-camera`, version `0.1.0rc1`.
Import: `pynq_acoustic_camera`. Python >=3.9 (the observed board uses 3.10).
The loader/configuration/installer use the standard library. Scientific and UI
modules inherited from the original project are imported lazily, with their
existing dependency declarations retained.

Pinned hardware: `SiririComun/hw-acoustic-camera`, `v0.1.0-rc.2`.
Manifest SHA-256:
`0d372c66a798cedcc4420caf800c612d34c7720a40f1480620c9809d9d421571`.
Source HW: `50b98d0ea8863c8cd8665fb6061b8cfb39222e0b`.
Interface: `legacy-xadc-reference/1`, runtime BUILD_ID absent/null.
This is an XADC distribution baseline, not a four-channel PDM driver.

## Installation from a branch — for the board integration step

Do not execute this on the board until the kernel and permissions are confirmed
in step 1.8. The intended command preserves the requested reinstall behavior:

```bash
/usr/local/share/pynq-venv/bin/python3 -m pip install \
  --force-reinstall --no-cache-dir --no-deps \
  'pynq-acoustic-camera @ git+https://github.com/SiririComun/sw-acoustic-camera.git@phase/01-baseline'
```

For reproducibility, substitute the recorded full SW commit for the branch.
`--no-deps` applies to runtime dependencies; build isolation can still acquire
setuptools/wheel from `pyproject.toml`. `--no-cache-dir` controls pip's cache and
does not purge the overlay cache. Installation and package import do not program
hardware. Restart the notebook kernel after reinstalling. Record `sys.executable`,
distribution version, module path and the VCS commit in installed `direct_url.json`;
version alone does not identify a moving branch commit.

## Installed resources

The sole runtime hardware configuration is
`pynq_acoustic_camera/data/hardware.json`, read using `importlib.resources`.
No configuration is searched in the working directory, repository root, or old
package. Missing/invalid configuration raises `HardwareConfigError`.
`setup.py` includes resource paths inside the package only. Profile JSON and the
historical notebooks are under the same `data/` tree. Wheel/sdist contents and an installation outside the checkout were checked in
step 1.7; see [verification](VERIFYING_DISTRIBUTION.md) and the current handoff.

Example resource access without relying on the checkout:

```python
from importlib import resources
import json
profile = json.loads(resources.files('pynq_acoustic_camera.data')
                     .joinpath('profiles').joinpath('active_buzzer_profile.json').read_text())
```

## Acquisition and explicit programming

```python
from pynq_acoustic_camera import HardwareLoader

# Download and verify only. This does not program FPGA.
bit_path = HardwareLoader.get_overlay_path()
manifest = HardwareLoader.get_manifest()

# Explicit board operation, reserved for step 1.8:
# overlay = HardwareLoader.load_overlay(board='pynq_z2')
```

The manifest's pinned SHA-256 is checked before artifact downloads. Repository,
board, FPGA part, source commit, top, interface ID, filenames, sizes and BIT/HWH
hashes must match. The HWH must describe the three-DMA XADC reference. The loader
currently rejects other interfaces; future PDM support needs an explicit update.
An alternate `version` argument is accepted only if it equals the installed pin.
No latest-release selection or fallback to the original repositories exists.

`load_overlay` checks board identity, imports PYNQ explicitly and revalidates the
pair immediately before constructing a generic `pynq.Overlay`. It does not invoke
the custom XADC driver's register setup/priming. Normal PYNQ IP initialization
can still occur. Unknown board detection requires explicit confirmation via
`board='pynq_z2'`; a detected conflicting board is rejected.

## Overlay cache and recovery

```text
${XDG_CACHE_HOME:-~/.cache}/pynq_acoustic_camera/
  SiririComun/hw-acoustic-camera/pynq_z2/v0.1.0-rc.2/<manifest_sha256>/
```

An explicit `download_dir` is a cache base: `pynq_acoustic_camera` and the full
identity are appended. It is not the direct BIT folder. Version/components are
validated before path construction. Each reuse verifies the manifest and both
artifacts; file presence or size alone is insufficient. An invalid existing
entry raises `HardwareIntegrityError` and does not program FPGA.

For an explicit replacement of this exact entry:

```python
bit_path = HardwareLoader.get_overlay_path(force_download=True)
```

Downloads use a temporary directory next to the entry. Only a complete verified
pair is promoted. A Linux process lock protects validation, replacement and
Overlay construction. A failed forced download leaves a previous complete entry
in place. No global cache deletion and no writes to the original project's cache.

## Preserving notebooks and legacy routines

`pynq-acoustic-camera-notebooks [destination]` installs only missing notebook
files in a dedicated destination and preserves every existing file. It never
purges notebooks or evidence. The baseline notebook is included for explicit execution in 1.8.
`--include-legacy` copies inherited examples under `legacy/`; they are unvalidated
against this candidate and are excluded by default.

Scientific routines remain under the new namespace; algorithm bodies were not
redesigned here. Existing test imports were migrated, but no tests were added or
run in this step. `MicrophoneArrayOverlay` remains an inherited XADC driver,
requires explicit `initialize_legacy=True`, verified pinned cache and confirmed
BOARD, and is not the default phase-1 loader or a generic PDM driver.

## Pending verification

Step 1.7 supplies local build/install checks, installed resources, import provenance,
download/cache rejection cases and the unexecuted baseline notebook. Step 1.8: real kernel,
permissions, reinstall, explicit programming and notebook execution. No claim
of board validation is made by the changes in 1.6.

Resource packaging follows [setuptools package-data guidance](https://setuptools.pypa.io/en/stable/userguide/datafiles.html). Python resource access uses [importlib.resources](https://docs.python.org/3/library/importlib.resources.html).
