# Fase 12.4: ensayo técnico en LaZ2

Se usa el BIT/HWH combinado de fase 11 (`base.bit` SHA-256
`22283318f3658de6fa64b63772e307e43bbabf87a7adfb6e4956baba325df0e7`).
El par, el código y el lanzador están preparados en
`/home/xilinx/phase12_candidate_20261122`. No se recompila PL ni se altera el
orden de los cuatro canales PCM. El modo de una o dos fuentes se selecciona en
el PS con `--max-sources 1` o `2`; las frecuencias del ensayo son manuales.

La ejecución necesita la C920, HDMI OUT y la matriz de micrófonos conectados
como en fase 11. El audio de PC reproduce simultáneamente 1550 Hz por el canal
izquierdo y 2350 Hz por el derecho. Es un estímulo técnico, no una medición de
dirección física calibrada. El mapa micrófono→canal PCM sigue configurable y
sin confirmar. El FOV 70°×50° también es provisional.

En una terminal interactiva del PC, desde el repositorio SW:

```bash
python3 tools/phase12_launch_from_pc.py --max-sources 2 --view combined
```

El lanzador abre SSH y `sudo` pide la contraseña **en esa terminal**. No se
envía ni guarda la contraseña. El runner verifica y recarga explícitamente la
Base oficial antes de descargar el candidato, y la restaura al terminar o al
fallar. Al aparecer `PHASE12_LIVE_READY`, el PC reproduce el WAV durante 34 s a
35 % de volumen de flujo, restaura el estado previo de mute y copia `result.json`,
`events.jsonl`, `board.log` y los cuadros PS enviados a HDMI a
`evidence/phase-12-step-04/board-live-<UTC del PC>/`.

Para comprobar posteriormente el modo de una fuente, usar
`--max-sources 1 --view combined`; ese modo fija 1550 Hz y el mismo lanzador
crea un segundo directorio de evidencia. `--view source0` y
`--view source1` seleccionan los mapas individuales en HDMI en modo de dos
fuentes. El modo automático se obtiene invocando el runner de placa sin
`--manual-frequencies`.

Evaluar por separado: continuidad de PCM, overflows/clips, tiempo de proceso
frente al período de descriptor, número de cuadros HDMI, detección de ambas
frecuencias, AoA válidos por ID y cuadros con 0/1/2 mapas visibles. La ejecución
no acepta por sí sola identidad de canales, ubicación física ni coincidencia
del mapa con un altavoz; eso permanece en las fases 9, 10.4 y 12.5.
