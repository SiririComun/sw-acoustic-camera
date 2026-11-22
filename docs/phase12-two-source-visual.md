# Fase 12.3 — mapas provisionales de dos fuentes en PC

Estado: composición y demo sintética comprobadas en PC. La ruta en vivo para
LaZ2 está preparada en `tools/phase12_live_camera.py`, pero todavía no se ha
ejecutado con PCM, C920 ni HDMI OUT en placa.

`TwoSourceVisualState` retiene solo el último AoA `VALID_PROVISIONAL` de cada
ID activo durante un máximo de 0,5 s. Una actualización omitida puede usar
ese AoA reciente; un rechazo, fin de evento o discontinuidad PCM borra su
capa. `render_two_source_maps()` proyecta las direcciones con el mismo modelo
provisional de FOV usado en fase 11 y dibuja dos gaussianas de colores
distintos sobre copias BGR del cuadro. Devuelve una vista por fuente, la
combinada y, cuando se solicitan, los mapas numéricos. Una dirección fuera del
cuadro no produce mapa. El arreglo concéntrico de 50 × 50 mm es una medida
reportada por el usuario; el mapa micrófono→canal y el FOV siguen provisionales.

La ruta de video del runner omite mapas completos y compone solo las regiones
con señal. En un PC, 100 cuadros de 640×480 con dos fuentes dieron p50 1,994
ms, p95 3,300 ms y máximo 4,540 ms de CPU del compositor por cuadro. La
versión inicial de cuadro completo dio p95 27,501 ms; el PNG combinado de la
demo optimizada tiene el mismo SHA-256 que el PNG anterior. Estas cifras no
incluyen captura, DMA, AoA ni envío HDMI y no predicen el ARM de la PYNQ.

## Demo reproducible

Requiere NumPy y OpenCV en PC. Genera dos ondas planas sintéticas, alimenta
el pipeline 12.2 y exporta un cuadro de prueba, dos capas y el combinado:

```bash
python tools/phase12_visual_demo.py --output /ruta/nueva/pc-two-source
```

Para mostrar solo una fuente configurada manualmente:

```bash
python tools/phase12_visual_demo.py --output /ruta/nueva/pc-one-source \
  --max-sources 1 --manual-frequencies 2350
```

La evidencia de esta ejecución está en
`../../evidence/phase-12-step-03/README.md`. Se generaron las capas de 1550 y
2350 Hz y la combinada, con proyecciones provisionales a `(107,349)` y
`(563,102)` en 640×480. Son posiciones del generador sintético, no
localización física comprobada.

## Runner preparado para 12.4

`tools/phase12_live_camera.py` conserva la ruta de PCM continuo y C920→PS→HDMI
de fase 11. Permite `--max-sources 1|2`, `--manual-frequencies`,
`--pcm-channels` y `--view combined|source0|source1`. Usa el BIT/HWH combinado
de fase 11 y un ajuste AoA por ventana para limitar picos de carga. El runner
solo pasó importación/`--help` y pruebas host de selección de vista; no se
programó la placa. La integración técnica y medición de FPS/cola corresponden
a 12.4. Fases 9 y 10.4 continúan abiertas para aceptación acústica física.
