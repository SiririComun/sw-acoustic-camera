# Notebook resources

`01_baseline.ipynb` is prepared in step 1.7 for execution in 1.8. It is shipped
without outputs and with FPGA programming disabled. Its presence does not mean
that the board has passed a baseline test.

`02_clock_control.ipynb` is a phase-2 PDM candidate notebook, prepared but not
executed. It validates the local candidate manifest and BIT/HWH pair first.
`PROGRAM_PDM_CANDIDATE` defaults to `False`; programming requires enabling that
flag and confirming `pynq_z2` explicitly. The clock exercise also defaults to
`False`. The notebook does not connect microphones or capture `DAT`; four-channel
capture belongs to phase 3.

The `legacy/` notebooks were inherited from the two-channel XADC sound localizer.
Their imports use the new package name, but their scientific workflows and hardware
initialization have not been validated against the new release. They are excluded
from the default notebook installation. Use `--include-legacy` only deliberately;
the legacy driver now requires `initialize_legacy=True` before it configures XADC,
DMA and triggers. These notebooks are not four-channel PDM examples.

`04_pcm_capture.ipynb` is prepared in4.5 for later authorized board runs. All
hardware flags default toFalse, outputs are cleared and BIT/HWH hashes are unset
until the4.6 candidate. It attaches without FPGA programming and prepares RAW
wiring activity, PCM patterns, tone/level measurements, identity and six-pair
delays. Read PCM_CAPTURE.md before enabling a physical session.
