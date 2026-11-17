# Fase 6.4 — revisión estática de convivencia PCM/video

Fecha: 2026-11-17. Estado: **revisión estática completada; convivencia en
ejecución y bitstream combinado no probados**.

## Fuentes revisadas

- Block Design PCM aceptado: `hw-acoustic-camera/src/bd/pdm_pcm_capture.bd`
  (SHA-256 `40f8ccb6be80e597422272a63e3d48e2352322238da04f6884c71c9f49ccd7fc`),
  manifiesto del candidato de fase 4 y cierre `context/PHASE4_CLOSURE.md`.
- Interfaz SW PCM: `sw-acoustic-camera/docs/pcm-hardware-interface.json`.
- Fuente upstream del overlay PYNQ-Z2: PYNQ tag `v3.1`, commit
  `19ed17d4fd4ea71f95a97d30f8ccd0d62b8dbaaa`, archivos
  [`base.tcl`](https://github.com/Xilinx/PYNQ/blob/19ed17d4fd4ea71f95a97d30f8ccd0d62b8dbaaa/boards/Pynq-Z2/base/base.tcl)
  y [`base.py`](https://github.com/Xilinx/PYNQ/blob/19ed17d4fd4ea71f95a97d30f8ccd0d62b8dbaaa/boards/Pynq-Z2/base/base.py).
  La carpeta base no presenta cambios entre tags upstream `v3.1` y `v3.1.2`.
- Evidencia de carga del `base.bit` instalado y loopback funcional:
  `evidence/phase-06-step-02/hdmi-loopback.md` (hashes del BIT/HWH guardados
  allí). El HWH/BD exacto instalado no está archivado en el workspace, así que
  la revisión del fuente upstream no prueba identidad binaria con el overlay
  que se ejecutó en placa.

## Resultado

1. **No se pueden mantener activos dos overlays completos cargándolos uno
   detrás de otro.** `pdm_pcm_capture` y el `base` de video describen diseños
   completos distintos para el PL. `Overlay.download()` programa el bitstream
   seleccionado y reemplaza la lógica anterior. El loopback de fase 6.2 probó
   el `base`; no cargó ni probó PCM.
2. **El `base` no proporciona una entrada USB UVC de cámara.** Su API de video
   presenta HDMI IN/OUT; la C920 se captura por UVC/V4L2 en Linux/PS. El cuadro
   debe pasar por DDR/PS y entregarse al camino de salida del overlay.
3. **Conflicto de puerto de memoria:** el VDMA de video del `base` tiene canales
   MM2S y S2MM y ambos llegan al HP0 del PS a través de su interconexión de
   memoria. El AXI DMA S2MM PCM también llega al HP0. Para usarlos juntos se
   necesita arbitraje/interconexión de ambos productores y clocking compatible,
   o asignar uno de los flujos a otro puerto HP habilitado en el diseño unido.
   No se midió el ancho de banda bajo carga conjunta.
4. **Conflicto de reloj en HP0:** el PCM actual conecta `S_AXI_HP0_ACLK` a
   `FCLK_CLK0` de 100 MHz. El `base` upstream conecta HP0 a `FCLK_CLK1` de
   142 MHz y usa otros dominios para video. El diseño combinado debe escoger y
   propagar clocks/reset coherentes, añadiendo conversión de dominio si hace
   falta; no se deben unir salidas de reloj distintas directamente.
5. **Conflicto de dirección AXI-Lite:** el control PCM está asignado a
   `0x43C00000`, igual que `audio_codec_ctrl_0` en el `base`. Hay que remapear
   uno de los dos bloques o retirar el códec si no se va a usar. El DMA PCM
   aparece en `0x40400000`; no coincide con la dirección VDMA del `base`
   (`0x43000000`).
6. **Conflicto de pines Pmod B:** el candidato PCM usa Pmod B para
   `pdm_clk_o` y cuatro entradas PDM. El `base` conecta esos pines a su IOP
   Pmod B. El diseño unido debe liberar esos pines del IOP y asignarlos al
   capturador PDM; no se pueden dejar dos controladores sobre el mismo pin.
7. **El PCM no necesita otro PS ni otra DDR.** Se debe construir un solo Block
   Design con una instancia de PS/DDR, los periféricos HDMI/VDMA necesarios y
   la IP PCM. También hay que reconciliar el mapa completo de direcciones,
   resets, clocks e interrupciones y volver a implementar timing/DRC.

## Decisión de integración

La ruta candidata requiere **un bitstream combinado** que reúna video HDMI y
captura PCM. La C920 sigue entrando por el PS mediante UVC; no se conecta
directamente al PL por el USB. Para el primer prototipo conviene conservar la
captura y composición de cuadros en PS y usar la VDMA del PL para HDMI, si el
rendimiento medido lo permite.

La revisión responde la pregunta estática de convivencia: overlays separados
son mutuamente excluyentes en el PL; sus funciones pueden convivir en un único
diseño tras resolver los conflictos anteriores. No se fusionaron Block Designs,
no se construyó un bitstream combinado y no se programó la placa. Por ello no
se afirma coexistencia funcional ni rendimiento conjunto.

## Trabajo pendiente cuando vuelva a estar estable la placa

- Completar 6.3: repetir captura C920→HDMI OUT→HDMI IN y obtener métricas
  válidas de FPS, recuperación, pérdidas observables y latencia.
- Si se continúa a integración conjunta, crear y revisar un único BD; asignar
  direcciones únicas; resolver HP0/arbitraje/clocking y liberar Pmod B.
- Implementar y revisar el bitstream, exportar el PDF real de Vivado, cargar
  ese único diseño y probar PCM y video simultáneamente bajo carga.

Este trabajo no cambió el contrato PCM ni construyó hardware. La política de
fase conserva commits locales y publicación HW/SW al cierre; no se publicó
ninguna rama.
