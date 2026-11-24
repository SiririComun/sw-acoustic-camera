# Preparación de descarga de trabajo al PL

Actualizado: 2026-11-23. Estado: **candidato parcial sin construir ni validar**.

## Primer corte implementado en fuente

El primer bloque mueve la composición de píxeles al PL. La webcam continúa
entrando por USB/UVC al PS porque el controlador de la C920 pertenece a Linux.
El PS escribe el cuadro sin modificar en DDR y el VDMA del PL lo lee. El nuevo
`acoustic_video_marker` va después del desempaquetador AXI4-Stream de 24 bits
(`pixel_unpack`) y antes del registro de HDMI OUT, donde cada beat ya representa
un píxel; reemplaza por rojo los píxeles cercanos a la coordenada enviada por
el PS.

El PS conserva la captura PCM, el detector tonal, el cálculo de fase, el AoA y
la proyección angular a píxeles. Envía una sola palabra de configuración al
GPIO AXI-Lite solo cuando cambia el punto. El PL toma una copia de esa palabra
al inicio de cada cuadro, para que el marcador no cambie de posición durante
un mismo cuadro. El orden y los datos PCM continúan intactos.

### Contrato del marcador PL

La palabra de 32 bits en `acoustic_marker_gpio` tiene este formato:

| Bits | Campo | Significado |
|---|---|---|
| 31 | enable | `1` habilita el marcador; `0` deja pasar el video intacto |
| 25:20 | radius | radio aproximado en píxeles; la máscara usa un octágono sin multiplicadores |
| 19:10 | y | fila del centro, en píxeles |
| 9:0 | x | columna del centro, en píxeles |

El GPIO está previsto en `0x43C90000`. El runner `phase11_live_camera.py`
admite `--marker-backend ps` para el BIT/HWH existente y
`--marker-backend pl` para el candidato que incluya el nuevo GPIO. Los PNG
exportados desde PS en el modo PL no incluyen el punto; el registro JSON guarda
la coordenada solicitada.

## Trabajo que sigue en PS

El candidato todavía no mueve detección tonal, fase ni AoA al PL. Esas tareas
son el mayor coste conocido: en la corrida de fase 11 una estimación AoA tardó
86,5 ms de media en la Zynq-7000 y el p95 de procesamiento de paquete fue
161,7 ms, frente a un período nominal de paquete de 85,3 ms. La corrida de 30 s
no perdió PCM, pero no constituye una garantía de tiempo real duro.

La siguiente pieza de audio debe observar las tramas PCM ya filtradas en una
ruta paralela al DMA actual, con almacenamiento crudo preservado y sin añadir
backpressure a la captura. La interfaz debe admitir geometría y mapa Mic→PCM
configurables; el firmware no debe reordenar las columnas PCM. La detección y
estimación de fase pueden producir frecuencias y los seis fasores de pares;
el ajuste conjunto de dirección requiere una implementación que respete los
pares diagonales, las ambigüedades de fase y la geometría real. No se debe
copiar al PL la geometría nominal como si fuera medida ni cerrar calibración
sin las posiciones conocidas y la validación independiente acordadas.

## Estado de verificación

- La fuente RTL, el cambio de Block Design/Tcl y el control opcional del runner
  están preparados en las ramas actuales.
- No se ha ejecutado Vivado, síntesis, simulación ni pruebas SW para este
  candidato en esta sesión. No se creó BIT/HWH nuevo.
- La carga en placa, el color/canal real del píxel, el ritmo de cuadros, la
  convivencia PCM/video y cualquier mejora de latencia quedan para la
  validación física indicada por el usuario.
- La geometría, el mapa acústico y la precisión espacial siguen provisionales.
