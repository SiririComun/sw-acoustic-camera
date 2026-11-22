# Fase 12.2 — dos tonos y dos AoA provisionales en PC

Estado: prototipo sintético de cuatro canales a 48 kHz. No se ejecutó en la
PYNQ ni acepta localización acústica real.

`TwoSourcePcmPipeline` recibe los paquetes desprendidos de
`ContinuousPcmCapture.read_packet()` (`int32 (N,4)` signed-24 y metadatos de
secuencia, trama, tasa y origen). Usa `StreamingTwoToneDetector` de 12.1 y
`SingleSourceAoAEstimator` de fase 10, evaluado por separado para cada
frecuencia activa sobre la misma ventana PCM. El mapa puerto→columna PCM y las
coordenadas se configuran mediante `AoAConfig`; el PCM original nunca se
reordena. El cuadrado concéntrico de 50 × 50 mm es una medida reportada por el
usuario, y la identidad acústica del mapa sigue sin confirmar.

```python
from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator
from pynq_acoustic_camera.two_source_pipeline import TwoSourcePcmPipeline
from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig

pipeline = TwoSourcePcmPipeline(
    detector=StreamingTwoToneDetector(TwoToneConfig(max_sources=2)),
    estimator=SingleSourceAoAEstimator(AoAConfig(pcm_channels=(0, 1, 2, 3))),
)
rows = pipeline.process_packet(continuous_pcm_packet)
```

Para una sola fuente se puede configurar `max_sources=1`; con
`manual_frequencies_hz=(1550,)` se fija además la frecuencia buscada. La API
existente de una fuente de fase 11 permanece disponible.

Por defecto el pipeline limita el cálculo a **un ajuste AoA por ventana** y
actualiza cada ID como máximo una vez cada 16 ventanas. Las fuentes pendientes
se turnan, por lo que una fuente nueva no espera a que se renueve la otra. En
PC, `max_fits_per_window=2` y `aoa_update_stride=1` permiten comprobar ambas
direcciones en la misma ventana. Cada fila expone solo el AoA de esa ventana;
una fuente ausente, una actualización omitida o un ajuste rechazado tienen
`direction=None`. Un hueco en paquetes reinicia detector, historia y agenda
AoA antes de continuar con un segmento nuevo.

Las pruebas sintéticas cubren dos ondas planas con frecuencias y direcciones
distintas, modos de una/dos fuentes, mapa de canales permutado, límite de
cálculo, liberación, fase inconsistente y discontinuidad PCM. Con 1550 Hz a
(+25°, −12°) y 2350 Hz a (−28°, +15°), ambos AoA fueron
`VALID_PROVISIONAL` dentro de 2° de los valores del generador. Esto mide
consistencia matemática con el mismo modelo usado para generar las señales;
no mide precisión física.

En un benchmark de PC de 120 paquetes sintéticos de 4096 tramas, el perfil por
defecto con cuatro semillas AoA produjo 60 ajustes, todos
`VALID_PROVISIONAL`; CPU del pipeline por paquete: p50 2.276 ms, p95 8.278 ms,
máximo 9.486 ms. La generación de PCM quedó fuera del cronómetro. El coste en
ARM, la cola DMA y el FPS con video siguen sin medirse para dos fuentes.

Fases 9 y 10.4 siguen abiertas para identidad acústica, geometría y calibración
física. La visualización de dos capas en cuadros de PC corresponde a 12.3.
