# Fase 12 — estado de cierre parcial

La fase amplía detección, localización provisional y visualización a dos tonos
en paralelo. Este documento registra el alcance que sí está demostrado y los
criterios que siguen pendientes. **La fase 12 no está cerrada.**

## Estado por subpaso

| Subpaso | Estado | Comprobación y evidencia | Límite actual |
|---|---|---|---|
| 12.1, detector configurable | Prototipo SW completado en PC | Modos automático/manual y máximo de una/dos fuentes; casos de silencio, ruido, estabilidad y separación sintética | Sin aceptación acústica física |
| 12.2, dos AoA | Prototipo SW completado en PC | Dos wavefronts sintéticos generan AoA independientes; una fuente puede seleccionarse sin alterar el orden de canales | Geometría y mapa de canales son provisionales |
| 12.3, mapas | Prototipo SW completado en PC | Mapas individuales y combinado sobre video sintético | Sin medición espacial calibrada |
| 12.4, integración LaZ2 | Parcial | PCM/AoA dual en audio-only por 45 s; ensayos conjuntos anteriores enviaron cuadros PS con dos mapas | La C920 desapareció del USB en la ejecución conjunta; falta operación sostenida PCM+C920+HDMI |
| 12.5, aceptación física | Pendiente, no aceptada | Hay ensayos exploratorios con pulsos separados por parlante | Requiere geometría/mapa de canal confirmados en fases 9/10.4 y criterios físicos acordados |

## Evidencia de integración en placa

El ensayo audio-only usó el BIT/HWH combinado de fase 11 sin reconstrucción y
no abrió C920 ni HDMI. Recibió 528 paquetes, devolvió 526 y drenó dos durante
la detención normal. Hubo cero discontinuidades, overflows y clips. Se
registraron diez AoA `VALID_PROVISIONAL` por fuente durante diez pulsos
estéreo de 1550 Hz a la izquierda y 2350 Hz a la derecha. El p95 de proceso
fue 58,58 ms; doce paquetes excedieron el período nominal de 85,33 ms y el
máximo fue 178,65 ms. Base se restauró y el PC quedó silenciado.

La ejecución conjunta más reciente, después de reconectar la webcam y reiniciar
LaZ2, alcanzó 266 paquetes, cinco AoA válidos por fuente y al menos 181 cuadros
HDMI antes de que `camera.read()` quedara bloqueado 10 s. La cámara desapareció
del bus USB con errores `cannot reset` y `-71`. Una captura aislada de C920
había entregado 241 cuadros durante 20 s después del reinicio; esa estabilidad
no se mantuvo durante la ejecución conjunta. El wrapper restauró Base tras los
fallos.

Los registros y capturas están en
[`evidence/phase-12-step-04`](../../evidence/phase-12-step-04/README.md),
incluidos el [ensayo audio-only](../../evidence/phase-12-step-04/audio-only-20261122T225348Z/)
y las [pruebas conjuntas](../../evidence/phase-12-step-04/board-live-20261122T221804Z-pulsed-after-reboot/).

## Verificación de software

El 2026-11-22 pasaron 68 pruebas host de `test_two_tone.py`,
`test_two_source_pipeline.py`, `test_two_source_visual.py` y
`test_single_source_aoa.py`. También pasaron `py_compile` de los runners nuevos
y `--help` del lanzador. La ruta audio-only corrió en la PYNQ con el resultado
anterior. El último commit local de SW es
`7f568ee6758ef192edef649e9f77f08700e481e2` en `phase/12-two-source`; aún no se
publica. HW no cambió de su rama publicada de fase 11.

Los dos scripts exploratorios de fase 10 sin seguimiento en SW se conservan
sin cambios.

## Pendiente para cierre completo

1. Recuperar una conexión USB estable para C920 y repetir una ejecución
   sostenida de PCM, cámara y HDMI con dos pulsos separados.
2. Confirmar cuadros HDMI con los dos mapas activos durante la operación
   simultánea; medir pérdidas, FPS y tiempo de procesamiento frente al período.
3. Completar identidad de canales/geometría en fases 9/10.4 y acordar criterios
   antes de la campaña de aceptación física 12.5.

Los resultados AoA y las posiciones dibujadas son provisionales. No se acepta
la identidad acústica, el error angular, la superposición con el campo de
cámara ni el cierre de 12.4 o 12.5.
