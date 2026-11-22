# Fase 10.2 — prototipo AoA sintético de una fuente

Estado: **prototipo comprobado en PC con señales sintéticas**, sin aceptación
física. Contrato de entrada, ejes y mapa en [`phase10-aoa-contract.md`](phase10-aoa-contract.md).

## Implementación

`pynq_acoustic_camera.single_source_aoa` añade `AoAConfig` y
`SingleSourceAoAEstimator`. `estimate()` recibe una ventana PCM `int32 (N,4)`
signed-24, tasa de muestreo y frecuencia explícitas, y opcionalmente índices
de trama y ventana. No carga overlays ni modifica el orden PCM. La
configuración contiene las coordenadas de los cuatro puertos, la permutación
puerto→columna PCM, su procedencia, velocidad del sonido y umbrales digitales.
El valor predeterminado del cuadrado de 50 × 50 mm y el mapa Mic1→0,
Mic2→1, Mic3→2, Mic4→3 son **reportes provisionales**, no medidas aceptadas.

Cada canal se normaliza a escala digital, se centra y se proyecta en la
frecuencia solicitada con ventana Hann. Una puerta de nivel y prominencia
espectral evita devolver direcciones a partir de silencio o ruido sin tono.
Con los seis pares, el ajuste busca una dirección unitaria en el hemisferio
frontal que minimice el error **circular** de las fases. Una búsqueda acotada
con refinamiento compara mínimos separados para señalar aliasing; se devuelve
`AMBIGUOUS_PHASE` cuando dos direcciones explican casi igual las fases. El
residuo de cada par queda en el resultado para diagnóstico.

Los umbrales predeterminados (`-80 dBFS`, 10 dB de prominencia, 0,25 rad de
residuo y 0,05 rad de margen entre soluciones) son parámetros digitales del
prototipo. No son sensibilidad, precisión angular ni fiabilidad física
aceptadas. El estado `VALID_PROVISIONAL` se mantiene incluso si el llamador
etiqueta su configuración de otra forma; `VALID_CALIBRATED` queda reservado
para el trabajo físico posterior. Un `VALID_PROVISIONAL` solo expresa que la
ventana satisfizo las condiciones digitales del modelo suministrado.

## Comprobación en PC

`tests/test_single_source_aoa.py` crea ondas planas de cuatro puertos y
cuantiza el resultado a signed-24 PCM en columnas físicas fijas. La matriz
cubre centro, signos de los ejes, cuadrantes y direcciones de 70°; frecuencias
de 1000, 1877 y 3000 Hz; ruido de 20 dB SNR y ganancias distintas; mapa de
canales permutado sin tocar el PCM; coordenadas no coplanares; silencio, ruido
de banda ancha, canal ausente, fase inconsistente y geometría con aliasing.
También comprueba tasa de muestreo distinta de 48 kHz nominal, frecuencia
incorrecta, geometría degenerada, entrada inválida y el sesgo que puede
introducir una velocidad del sonido incorrecta.

Comando: `python -m pytest -q tests/test_single_source_aoa.py` (**43 passed**).
El generador
conoce la dirección de origen y no utiliza la fase 9.3 como verdad de terreno.
No se ensayaron PCM físicos ni se midió carga de CPU en placa en 10.2.

## Límites y siguiente dependencia

La geometría coplanar no distingue delante de detrás: el hemisferio frontal
es un supuesto del producto. Reflexiones, múltiples fuentes a la misma
frecuencia, retardos electrónicos y errores de posición, mapa o velocidad
del sonido pueden dar un resultado numéricamente coherente y físicamente
erróneo. El ajuste de fase por sí solo no calcula distancia ni ubicación en
la imagen de C920.

El siguiente subpaso propuesto es 10.3, enlace con ventanas del PCM continuo
y eventos del detector, con evaluación de coste en PC. Calibración y aceptación
física de fase 10 esperan las coordenadas/orientación y la identidad acústica
pendientes de fase 9; el límite de error angular se acordará antes de la
campaña física.
