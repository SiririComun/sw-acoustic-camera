# Fase 12 — caracterización sintética adicional en PC

Esta caracterización amplía las pruebas host de 12.1; no es un nuevo criterio
de cierre y no representa aceptación física. El script
[`phase12_pc_characterization.py`](../tools/phase12_pc_characterization.py)
genera PCM signed-24 determinista y barre separación tonal, relación de
amplitud y ruido blanco. Registra cada ensayo y su configuración en
[`evidencia`](../../evidence/phase-12-step-05/README.md).

En el barrido, ambos tonos de 1550 Hz y 1550 Hz + separación se recuperaron en
0/3 ensayos a 60, 80 y 100 Hz, y en 3/3 a 120 Hz o más. Para el par 1550/2350 Hz,
se recuperaron ambos tonos en las tres semillas en las 25 combinaciones de
ruido (sigma 0.001–0.03 de escala completa) y relación de amplitud (1:1–12:1).
Los controles de ruido solo no abrieron pistas; un tono no abrió una segunda.

Solo son resultados del detector en el modelo sintético escogido. No se
extrapolan a SNR acústico ni a habitaciones, y no fijan la separación mínima
física. Las fases 9 y 10.4, la integración sostenida C920/HDMI de 12.4 y la
aceptación acústica 12.5 siguen pendientes según sus propias condiciones.
