# PCM integration interface for step 4.5

The step-4.4 RTL target is specified in
[pcm-hardware-interface.json](pcm-hardware-interface.json). It has a new BUILD_ID
`B691CC9C9F6249AD938D2063E4767C54`, ABI 1.0 and capabilities 0xF. This is a source
interface target, not an installable overlay: BIT/HWH do not exist yet. Their
hashes and actual HWH parameters must be pinned after the step-4.6 build.

The existing phase-3 RAW loader/API/candidate remain the accepted baseline.
The next step must add PCM handling against this interface: FORMAT=1, `(N,4)`
int32, 16*N bytes, four sign-extended signed24 samples per frame. The simple
S2MM DMA must retain 23 length bits, 32-bit stream/master and PS HP0=64.

Physical PCM START requires CLOCK_READY and FILTER_READY. Pattern START needs
CLOCK_READY only; the pattern resets its index per block and yields `16*n+c`.
PCM_RATE_HZ=48000, DECIMATION=50, FILTER_SETTLE_FRAMES=6. CLIP_COUNT counts clipped
channel samples admitted into frame FIFO, not clipped frames. Pattern and RAW
blocks produce zero clips.

DMA-local reset clears the pending FIFO and stream serializer while preserving
clock configuration and settled filter history. The driver must retain errors
until successful recovery and CLEAR. START of a block must not restart filter
settling; stopping the PDM clock must reset it. The FIFO admits complete frames,
then emits channel words in order with a final-word-only TLAST. Error handling
must reject whole blocks, including any partial DMA payload.
