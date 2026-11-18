# Phase 7 continuous PCM software API

The phase-7 continuous hardware candidate is a separate ABI 1.1 build with
BUILD_ID `7A2C7F017A2C4E7D8F70070100000003`. It retains the register/stream
contract and reviewed source-domain status registers. AXI DMA's optional S2MM
status stream is disabled because this design has no status-stream producer;
without status data, the DMA does not mark received descriptors complete. The
phase-4 finite PCM candidate and its driver remain unchanged. This driver
attaches only to the phase-7 candidate and uses direct S2MM Scatter/Gather MMIO;
it does not instantiate PYNQ's simple-mode DMA driver.

## Host API

`ContinuousPcmCapture` owns the S2MM channel for one session. Construction is
read-only. `start()` allocates a descriptor array and PCM slot pool, fills the
64-byte S2MM descriptors, flushes descriptors to memory, invalidates DMA output
slots, starts S2MM, then starts the capture IP. The DMA cyclic-mode control bit
is left clear. The descriptor links wrap around the finite software-managed
ring; hardware may process a returned slot only after software advances
`TAILDESC` for that slot.

The default queue contains 64 slots of 4096 frames each. One packet is 65,536
bytes, and the pool uses 4 MiB (about 5.46 seconds of PCM at 48 kHz). The caller
must call `read_packet()` as packets complete. It invalidates only the completed
descriptor and slot, copies a detached `(N, 4)` `int32` array, checks the S2MM
completion flags, exact byte count and signed-24 range, and then returns that
slot to DMA. A
slow caller eventually exhausts the queue; FIFO overflow or a DMA error
invalidates the session.

For `source='pattern'`, each packet is checked against the continuous frame
index and channel order, including the 20-bit pattern wrap. For
`source='microphones'`, the filter-ready bit is required before START. The
physical microphone capture is not run by this host API verifier.

`stop()` asks the capture IP to stop at the next packet boundary, drains
completed descriptors without re-arming slots, and waits for DONE. The software
then soft-resets S2MM to retire any prefetched empty descriptors before it
checks the final 64-bit identities and releases buffers:

```text
frames_total * 16 == words_total * 4 == bytes_received
```

If counters, descriptor bytes, overflow flags, or sequence checks disagree, no
successful result is returned. DMA and capture waits have finite deadlines. A
failed DMA reset retains the CMA roots and marks the API unusable until the
overlay is reloaded, so memory is not released while DMA might still write it.

## Board attachment after Linux/PYNQ recovery

This is an API example for the later board step. `Overlay(...)` programs the
FPGA, so this example was not executed in phase 7.3:

```python
from pynq import Overlay
from pynq_acoustic_camera import ContinuousPcmCapture

overlay = Overlay('/path/to/pdm_pcm_continuous_capture.bit')
capture = ContinuousPcmCapture.from_overlay(overlay)
capture.enable_clock()
capture.start(source='pattern')       # transport check; microphones not needed
packet = capture.read_packet()
result = capture.stop()
```

`from_overlay()` checks the loaded HWH for S2MM Scatter/Gather, 32-bit sample
and descriptor addressing, the configured descriptor length, and 64-bit PS
HP0. Construction does not program the overlay or write MMIO. For physical
four-channel capture, use `source='microphones'`; that later intervention needs
all four microphones reported connected to Pmod B.

## Limits of this software step

The verifier uses host models only. It checks descriptor layout and ownership,
cache-sync call ordering, sequence across slot reuse, stop/drain accounting,
counter mismatch, overflow handling, and unsafe-buffer retention after a
failed reset. It does not establish that the DMA queue or cache maintenance has
worked on the PYNQ-Z2. The phase-7.4 CDC review is complete; the routed CDC
report retains bundled-data and reset/XPM classifications, with the two
functional status paths registered in their source domains. Board DMA and
cache-coherency validation remain pending. The API's BUILD_ID check and all
eight host-model checks passed against the updated identity in the 7.4 review;
the report is in `evidence/phase-07-step-04/software/`.

The implementation follows the [PYNQ 3.1 DMA documentation](https://pynq.readthedocs.io/en/v3.1/pynq_libraries/dma.html),
which states that its DMA class supports simple mode only, and its
[PynqBuffer API](https://pynq.readthedocs.io/en/v3.1/_modules/pynq/buffer.html)
for per-view cache synchronization. S2MM descriptor fields and tail-pointer
queue behavior follow [AMD PG021 descriptor fields](https://docs.amd.com/r/en-US/pg021_axi_dma/RX-Descriptor-Fields)
and [descriptor management](https://docs.amd.com/r/en-US/pg021_axi_dma/Descriptor-Management).
