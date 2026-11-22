# Fase 10.3 — enlace PCM continuo, detector y AoA en PC

Estado: integración de software comprobada con paquetes sintéticos. No se
conectó a la PYNQ-Z2 ni se verificó exactitud AoA física. La fase 9 sigue
abierta para geometría e identidad de canales.

## Flujo implementado

`SingleSourcePcmPipeline.process_packet()` acepta el formato desprendido de
`ContinuousPcmCapture.read_packet()`: `samples` `int32 (N,4)` y metadatos
`packet_index`, `first_frame`, `frames`, `sample_rate_hz`, `source`. Rechaza
fuente de patrón y frecuencia de muestreo incompatible con el detector. Exige
secuencia contigua de paquetes; ante una discontinuidad reinicia el detector
y avisa al llamador. El llamador puede iniciar un nuevo segmento después de
ese error, sin mezclar estado previo.

El detector de fase 8 fija la frecuencia de un evento mediante
`system.frequency_hz`. Solo cuando `system.present` está activo se pasa la
ventana PCM correspondiente al estimador de 10.2. El resultado por ventana
contiene el evento del detector y un resultado AoA, o `aoa=None` si no hay
evento. Un evento activo todavía puede producir `NO_TONE`, `AMBIGUOUS_PHASE`
u otro rechazo AoA; no se conserva un ángulo anterior como si fuera actual.
Los índices de trama, inicio y fin de evento se convierten a índices
absolutos de la captura, incluso si el segmento empezó después de la trama
cero. La frecuencia usada y el mapa configurado quedan en el resultado AoA.

El pipeline conserva entre paquetes a lo sumo `window_frames-1` tramas
(2047 predeterminadas). Combina temporalmente esa historia con el paquete
actual para reconstruir ventanas que cruzan el límite; no retiene sesiones
PCM completas ni modifica el bloque recibido. Con la configuración
predeterminada, el bloque temporal contiene como máximo 2047+4096 tramas.
Esta cota no incluye las asignaciones temporales de FFT y ajuste del estimador.

## Verificación y coste medido en PC

`python -m pytest -q tests/test_single_source_pipeline.py
tests/test_single_source_aoa.py` pasó **48/48** casos. Las pruebas del enlace
cubren eventos activos, ventanas repartidas entre paquetes de tamaños
distintos, índices absolutos, liberación del tono sin ángulo obsoleto, reset
tras un hueco, fuente de patrón y tasa incompatible.

El benchmark reproducible se ejecutó con:

`python -m tools.phase10_pipeline_benchmark --packets 120 --output
../evidence/phase-10-step-03/pc-synthetic-benchmark.json`

Usó una onda plana sintética de 1877 Hz a (azimut +25°, elevación −12°),
120 paquetes de 4096 tramas a 48 kHz nominales (10,24 s de audio). Produjo
479 ventanas del detector y 476 AoA `VALID_PROVISIONAL`; las tres ventanas
iniciales aún no tenían evento activo. Las medianas AoA fueron +25,008° y
−12,005°, medidas contra la dirección del generador sintético. El tiempo de
CPU del pipeline por paquete fue 41,51 ms mediana, 45,16 ms p95 y 126,64 ms
máximo; tiempo de pared p95, 45,02 ms. Un paquete de 120 superó en CPU el
período nominal de 85,33 ms. Resultado completo:
[`pc-synthetic-benchmark.json`](../../evidence/phase-10-step-03/pc-synthetic-benchmark.json).

Estas cifras corresponden al PC y al procesamiento sintético. No incluyen
lectura DMA, entorno PYNQ, I/O ni medición de cola en placa. Tampoco convierten
la dirección calculada en orientación real respecto a la C920. No se suma
este p95 al p95 físico de fase 8, porque proceden de máquinas y condiciones
distintas.

## Próxima dependencia

La calibración AoA física de 10.4 requiere coordenadas, profundidad,
orientación e identidad acústica confirmadas en fase 9. La segunda captura
POCO de 9.3 no cumple como verdad de terreno angular. El límite de error
angular se acordará antes de la campaña física, con posiciones de validación
distintas de las de calibración. No se inició integración PCM/video de fase 11.
