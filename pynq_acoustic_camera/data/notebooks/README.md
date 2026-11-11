# Notebook resources

`01_baseline.ipynb` will be prepared in phase-1 step 1.7. Its absence here does
not mean that the board or package has passed a baseline test.

The `legacy/` notebooks were inherited from the two-channel XADC sound localizer.
Their imports use the new package name, but their scientific workflows and hardware
initialization have not been validated against the new release. They are excluded
from the default notebook installation. Use `--include-legacy` only deliberately;
the legacy driver now requires `initialize_legacy=True` before it configures XADC,
DMA and triggers. These notebooks are not four-channel PDM examples.
