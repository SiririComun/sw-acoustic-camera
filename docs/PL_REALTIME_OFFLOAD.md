# Preparación de descarga de trabajo al PL

Actualizado: 2026-11-23. Estado: **BIT/HWH generado; pendiente de carga y prueba en placa**.

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
GPIO AXI-Lite solo cuando cambia el punto. El reloj del GPIO es FCLK3 y el del
video es FCLK1; el PL sincroniza la palabra con dos etapas `ASYNC_REG` y
actualiza los límites del marcador en el límite entre cuadros. El orden y los
datos PCM continúan intactos.

### Contrato del marcador PL

La palabra de 32 bits en `acoustic_marker_gpio` tiene este formato:

| Bits | Campo | Significado |
|---|---|---|
| 31 | enable | `1` habilita el marcador; `0` deja pasar el video intacto |
| 25:20 | radius | semiancho y semialto en píxeles; la máscara es una caja cuadrada |
| 19:10 | y | fila del centro, en píxeles |
| 9:0 | x | columna del centro, en píxeles |

El GPIO está previsto en `0x43C90000`. El runner `phase11_live_camera.py`
admite `--marker-backend ps` para el BIT/HWH existente y
`--marker-backend pl` para el candidato que incluya el nuevo GPIO. Los PNG
exportados desde PS en el modo PL no incluyen el punto; el registro JSON guarda
la coordenada solicitada.

## BIT/HWH para la prueba en placa

Vivado 2024.2.2 generó el candidato en
[`builds/phase11-combined-pl-marker-20261123-r5`](../../builds/phase11-combined-pl-marker-20261123-r5/).
La pareja `base.bit`/`base.hwh` está en esa carpeta y tiene un manifiesto con
hashes. La síntesis del marcador pasó sin errores; DRC reportó cero errores y
`make check_timing` pasó con WNS de `+0.385 ns` y cero endpoints de setup
fallidos. El reporte CDC clasifica las 27 rutas usadas GPIO→primera etapa como
`CDC-3` sincronizadas; la excepción de timing cubre solo ese cruce asíncrono.
Los caminos de píxel posteriores al sincronizador siguen bajo timing normal.

En la Zynq, después de copiar la pareja BIT/HWH y el checkout SW que incluye el
backend PL, ejecutar el runner con `--marker-backend pl`. Ejemplo; ajustar
`SW_ROOT` a la ubicación real del checkout en LaZ2:

```bash
SW_ROOT=/home/xilinx/acoustic-camera/sw-acoustic-camera
RUN_ROOT=/home/xilinx/phase11-pl-marker
sudo /usr/local/share/pynq-venv/bin/python3 "$SW_ROOT/tools/phase11_live_camera.py" \
  --bit "$RUN_ROOT/base.bit" \
  --output "$RUN_ROOT/run-pl-marker-01" \
  --duration-seconds 30 \
  --marker-backend pl
```

Antes de cargarlo, comprobar la alimentación real: fuente oficial de 12 V en
DC1, J9 en REG y placa arrancada. No cambiar J9 con la placa encendida. La
prueba busca confirmar imagen C920 por HDMI, marcador rojo controlado desde
GPIO y captura PCM simultánea; no calibra la posición angular ni valida
precisión acústica.

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

- La síntesis, implementación, enrutado y generación BIT/HWH se completaron.
- No se cargó el candidato en la placa; color, ritmo de cuadros y convivencia
  PCM/video todavía requieren la prueba física acordada.
- No se midió mejora de latencia; mover la composición al PL reduce el trabajo
  de dibujo por cuadro en PS, pero AoA continúa en PS.
- La geometría, el mapa acústico y la precisión espacial siguen provisionales.
