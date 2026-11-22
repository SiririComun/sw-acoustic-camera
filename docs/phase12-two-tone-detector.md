# Fase 12.1 — detector de hasta dos tonos en PC

Estado: prototipo SW comprobado con PCM sintético. No usa la PYNQ ni constituye
aceptación acústica de dos fuentes.

## Interfaz y modos

`StreamingTwoToneDetector` consume bloques PCM `int32 (N,4)` con muestras
signed-24 a 48 kHz. Retiene como máximo una ventana de 2048 tramas y rechaza
bloques mayores que 4096 tramas. Produce resultados cada 1024 tramas, con
índices de ventana, candidatos, hasta dos estados de fuente y eventos de inicio
o fin. Un ID permanece asociado a una frecuencia durante su evento; dos
ventanas ausentes liberan ese ID.

```python
from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig

automatic_two = StreamingTwoToneDetector()  # Hasta dos picos en 1–3 kHz.
automatic_one = StreamingTwoToneDetector(TwoToneConfig(max_sources=1))
manual_one = StreamingTwoToneDetector(TwoToneConfig(
    max_sources=1, manual_frequencies_hz=(1550,)))
manual_two = StreamingTwoToneDetector(TwoToneConfig(
    manual_frequencies_hz=(1550, 2350)))
rows = manual_two.process_block(pcm_int32_4096x4)
```

El detector anterior `StreamingSingleToneDetector` y el runtime de fase 11
siguen disponibles. La selección de modo en el runtime de cámara se integrará
en un subpaso posterior de fase 12; este subpaso solo implementa la API y el
algoritmo en PC.

En modo automático se buscan máximos locales de potencia FFT sumada de los
cuatro canales y se excluyen candidatos separados por menos de 100 Hz. En
modo manual se inspecciona una vecindad de ±50 Hz de cada frecuencia pedida.
Cada candidato debe superar nivel y prominencia en al menos tres canales.
La entrada requiere cuatro columnas en el orden PCM existente; el detector no
reordena canales. Los umbrales son parámetros de prototipo, no criterios de
aceptación física.

## Comprobación

`python -m pytest -q tests/test_two_tone.py` cubre silencio/ruido, cero, uno y
dos tonos, ambos modos, IDs estables, liberación, límites de entrada, bloques
partidos, tonos fuera de banda y separación. Dos tonos sintéticos de 1550 y
1670 Hz se resolvieron; 1550 y 1610 Hz no se declararon como dos con la
configuración inicial. Esto no establece una separación mínima física.

Como referencia de coste de PC, 120 paquetes sintéticos de 4096 tramas con
1550 y 2350 Hz dieron CPU del detector por paquete p50 2.132 ms, p95 2.277 ms
y máximo 7.584 ms. La generación de señal quedó fuera del cronómetro. No se
puede extrapolar esta medición al ARM de la PYNQ ni sumarla al AoA y video sin
una prueba conjunta.

## Límites

Dos componentes espectrales no prueban dos emisores físicos: un armónico,
reflejo u otra señal puede crear un segundo pico. Todavía no se ha medido la
tasa de falsos positivos ni la separación mínima tonal en placa. Fases 9 y
10.4 siguen abiertas; el mapa físico micrófono→canal no está confirmado. La
localización, visualización e integración de dos fuentes pertenecen a los
subpasos 12.2–12.5.
