# Fase 10.1 — contrato del prototipo AoA de una fuente

Estado: diseño para implementación en PC. No hay estimador nuevo ni resultado
AoA físico en este paso. La fase 9 sigue abierta para geometría e identidad.

## Base real y reutilización

`ContinuousPcmCapture.read_packet()` entrega `ContinuousPcmPacket.samples` como
`int32` de forma `(N, 4)`, con muestras signed-24 extendidas en signo, columnas
PCM 0–3 sin permutar y metadatos con `sample_rate_hz=48000` nominal,
`first_frame`, `frames` y `source`. Sus 4096 tramas predeterminadas duran
85,33 ms. Las ventanas AoA pueden cruzar paquetes; el consumidor debe conservar
la continuidad de índices y reiniciar su estado ante una pérdida o un error de
captura. El contrato no presupone frecuencia de muestreo exacta medida.

`StreamingSingleToneDetector` acepta esos bloques y emite resultados por
ventana, con `system.present` y `system.frequency_hz` cuando fija un evento.
Ese valor puede alimentar el AoA en la integración posterior, aunque la
aceptación acústica del detector de fase 8 tiene limitaciones registradas. El
estimador AoA también admitirá una frecuencia explícita para ensayos sintéticos.

`pcm_analysis._samples` valida forma, tipo y rango; su normalización digital es
reutilizable como regla de entrada. `pcm_spectrum` sirve para diagnóstico. La
función `estimate_pair_delays` devuelve seis retardos enteros con signo
documentado y es útil para inspeccionar ruido de banda ancha. A 48 kHz una
muestra son 20,83 µs; un lado nominal de 50 mm representa como máximo unas
7 muestras de diferencia de llegada. Ese helper no resuelve con precisión
submuestra un tono puro ni constituye por sí solo el localizador tonal.

`kinematics.KinematicAnalytics.speed_of_sound`, la proyección Fourier compleja
y la relación entre diferencia de fase, frecuencia, separación y velocidad del
sonido son referencias matemáticas aprovechables. `AngleOfArrivalEstimator`
trabaja con dos señales XADC en voltios, una separación escalar, puerta de
ruido en voltios y pruebas sintéticas a 50 kHz. Su API, valores por defecto y
`array.MicrophoneArrayOverlay.capture_aoa_frame()` pertenecen a la ruta XADC
antigua. No son entrada ni validación del PCM continuo. El campo heredado
`coherence` se calcula con un solo fasor complejo por canal: para amplitudes
no nulas su fórmula da 1 por construcción y no mide coherencia temporal.
No se reutilizará como criterio de calidad AoA.

## Geometría, canales y ejes

La vista de referencia es desde delante, mirando hacia la cámara: `x` positivo
a la derecha en esa vista, `y` hacia arriba y `z` desde la cámara hacia la
persona. Coordenadas en metros dentro del cálculo; la configuración puede
registrar milímetros y convertirlos explícitamente. El origen provisional es
el centro geométrico del cuadrado, no la lente. Un origen común no afecta las
diferencias de fase. La traslación respecto a la lente y la orientación real
del arreglo quedan pendientes de fase 9.

| Puerto reportado | Posición provisional `(x, y, z)` mm | Canal PCM reportado |
| --- | ---: | ---: |
| Mic1, superior izquierda | `(-25, +25, 0)` | 0 |
| Mic2, superior derecha | `(+25, +25, 0)` | 1 |
| Mic3, inferior derecha | `(+25, -25, 0)` | 2 |
| Mic4, inferior izquierda | `(-25, -25, 0)` | 3 |

Las cuatro posiciones y la asociación puerto→columna son parámetros
configurables con procedencia `reported_provisional`. El PCM siempre conserva
el orden de columnas 0, 1, 2, 3. Si fase 9 refuta el mapa, se cambia la
configuración y se conserva la evidencia cruda; no se reescribe el PCM. La
segunda toma POCO de 9.3 tuvo canal 2 más fuerte y temprano en M1–M4 y carece
de posición/orientación de fuente medidas: no es verdad de terreno AoA.

Una dirección frontal unitaria `u=(ux, uy, uz)` apunta desde el arreglo hacia
la fuente y tiene `uz>=0`. `ux>0` significa fuente a la derecha en la vista
frontal definida arriba. Se devolverán componentes `ux, uy, uz`, azimut
`atan2(ux, uz)` y elevación `atan2(uy, sqrt(ux²+uz²))`, en grados. La
transformación de estos ejes a píxeles de C920 es un problema de calibración
posterior; no se afirma superposición espacial en fase 10.

## Método candidato para 10.2

Para una ventana síncrona de los cuatro canales, eliminar DC, aplicar la misma
ventana Hann y proyectar cada columna sobre la frecuencia de trabajo `f` para
obtener un fasor complejo `q_i`. Convertir signed-24 a escala digital
`[-1,1)` antes de comparar niveles. Con una onda plana del frente, un puerto
desplazado hacia la fuente recibe la misma oscilación antes. La convención es:

`wrap(arg(q_j) - arg(q_i)) = wrap(2π f (r_j-r_i)·u / c)`.

La velocidad `c` se suministra o deriva de una temperatura indicada, siempre
registrando cuál se usó. El algoritmo debe usar las cuatro posiciones y
comprobar consistencia entre pares. Los lados nominales permiten estimar `ux`
y `uy`; pares diagonales aportan comprobación, pero pueden estar envueltos en
fase. El ajuste debe comparar residuos de fase circulares, evaluar soluciones
alternativas cuando haya aliasing y rechazar una dirección única si los datos
no la determinan. No se extrapolará el `arcsin` escalar heredado a cuatro
canales. Con micrófonos coplanares, el signo de `uz` es indistinguible por
audio: elegir el hemisferio frontal es una hipótesis explícita. Si
`ux²+uy²>1` fuera de la tolerancia numérica, el vector calculado es
físicamente inviable.

A 20 °C, `c≈343 m/s`. A 3 kHz la longitud de onda es aproximadamente 114 mm:
un lado de 50 mm queda por debajo de media longitud de onda (≈57 mm), pero
la diagonal de 70,7 mm la supera. Esta comparación nominal no certifica
ausencia de aliasing con geometría, temperatura y frecuencia reales. La
proyección tonal tampoco separa trayecto directo de reflexiones o fuentes
cofrecuenciales. La ganancia distinta de canales y los retardos electrónicos
desconocidos pueden sesgar la fase física; la calibración no se infiere de una
señal sintética.

## Interfaz propuesta

- Entrada: ventana `int32 (N,4)` signed-24, `sample_rate_hz` explícito,
  `frequency_hz` dentro de la banda de producto 1–3 kHz, configuración de
  cuatro posiciones y mapa puerto→canal, velocidad del sonido y estado de
  procedencia de la configuración. Los índices de trama acompañan la ventana
  cuando provenga de captura continua.
- Salida: dirección o `None`, azimut/elevación o `None`, frecuencia usada,
  índice de ventana, fases/niveles por canal, residuos y diagnósticos de pares,
  estado de calidad y procedencia geométrica. `None` expresa que no existe una
  dirección única defendible; no se sustituye con un ángulo cero.
- Estados mínimos: `VALID_PROVISIONAL`, `VALID_CALIBRATED`, `NO_TONE`, `INSUFFICIENT_CHANNELS`,
  `AMBIGUOUS_PHASE`, `INCONSISTENT_PAIRS` e `INVALID_GEOMETRY`. Los umbrales
  digitales de calidad serán parámetros declarados y comprobados en 10.2, no
  límites de precisión angular física inventados.
- La API no depende de `Overlay`, XADC, voltios, cámara o transporte DMA. La
  integración futura pasa la matriz de muestras y metadatos; el AoA no opera
  la placa ni reordena muestras.

## Comprobación prevista y aceptación

10.2 comprobará con PCM sintético `(N,4)` a 48 kHz nominales el centro,
cuadrantes, límites angulares, varias frecuencias entre 1 y 3 kHz, ruido,
silencio, pérdida de canal, fase ambigua y configuraciones permutadas de mapa
sin permutar la matriz PCM. La dirección generada será conocida por el
generador independiente. También se comprobará el signo de cada eje y el
efecto de usar frecuencia, temperatura o geometría incorrectas. Esta
comprobación solo cierra el prototipo en PC.

La aceptación física requiere primero los centros de puertos, profundidad y
orientación relativos a una referencia de la C920, así como identidad acústica
confirmada en fase 9. Se debe acordar el error angular aceptable antes de la
campaña, calibrar con posiciones registradas y validar con posiciones
independientes. Hasta entonces no hay exactitud física ni mapa sobre video
aceptados. No se incluye bitstream combinado ni integración de video en fase
10.
