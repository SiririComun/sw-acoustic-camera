# Verify phase-1 distribution locally

Step 1.7 uses disposable virtual environments outside the source checkout. It does
not connect to the board, execute the baseline notebook on hardware, or run Vivado.
The loader verification suite uses the standard library and a simulated Overlay.
The scientific algorithms' historical suites are outside this distribution check.

## Build and inspect

In an isolated build environment, install the `build` frontend and run:

```bash
python -m build --outdir /external/artifacts /path/to/source-snapshot
```

Inspect wheel and sdist for the installed configuration, profiles, baseline
notebook and ten opt-in legacy notebooks. Confirm the wheel does not contain the
old import namespace, Cog or tests. The wheel is produced from the sdist, which
also checks that its build inputs/resources are sufficient.

## Install outside the checkout

Create a new virtual environment without scientific packages or PYNQ. Install
the wheel with `--no-deps`, change into a directory outside the checkout, and run:

```bash
/path/to/venv/bin/python /path/to/source/tools/verify_distribution.py \
  --assets-dir /external/verified-release-assets \
  --report /external/new-verification-report.json --network
```

Fixtures are the previously verified BIT/HWH/build manifest from HW v0.1.0-rc.2.
`--network` additionally exercises one real download through the loader, in a
temporary cache. Without it the live-download case is skipped explicitly.

The suite checks installed provenance and lazy import, packaged resources and
notebook code syntax, missing/invalid configuration, rejected version overrides,
verified cache reuse without network, corruption/missing/linked artifact rejection,
complete promotion, explicit repair, failed download preservation, partial-download
rejection, simulated generic programming, board guards and preservation of notebook
edits/evidence. No real PYNQ module or FPGA is used by the programming simulation.

## Branch reinstall

Publish the selected SW commit on `phase/01-baseline`, then use another isolated
runtime environment and the exact requested flags:

```bash
/path/to/venv/bin/python -m pip install \
  --force-reinstall --no-cache-dir --no-deps \
  'pynq-acoustic-camera @ git+https://github.com/SiririComun/sw-acoustic-camera.git@phase/01-baseline'
```

Record `direct_url.json`'s VCS commit ID and requested revision, module location,
version and interpreter. Compare against the published branch. Repeat installation
and verify that copied notebook edits/evidence and the dedicated hardware cache
remain intact. Do not confuse local VCS installation with verification of the
Jupyter kernel or install permissions on the board.

## Results and limits

The first wheel verification passed 16 cases on local Python 3.13 without PYNQ or
scientific dependencies, including a real release download. Build/installation logs,
archive inventories, source commits, hashes and structured test reports are kept
in workspace `evidence/phase-01-step-07/`. Consult the current handoff for final
branch-reinstall results and the exact commit; no self-referential commit is
embedded in this document or the notebook.

The baseline notebook ships with empty outputs, `PROGRAM_FPGA=False` and an empty
`EXPECTED_SW_COMMIT` that must be filled from the current handoff in the installed
copy. Step 1.8 must verify the actual Python 3.10 PYNQ kernel, permissions, repeated
reinstallation and explicit programming, then save an executed notebook/report.
Local simulation does not prove hardware behavior or accept the full phase.
