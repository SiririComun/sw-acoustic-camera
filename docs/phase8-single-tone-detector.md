# Fase 8.2 — prototipo SW del detector de un tono

## Alcance de este paso

Prototipo local en PC para PCM firmado de 24 bits dentro de `int32`, cuatro canales y 48 kHz. La banda inicial configurable es 1–3 kHz. No se integró aún con la captura de la PYNQ, no se ejecutó DSP en placa y no se procesó audio físico.

## Método candidato

- FFT real Hann sobre ventanas de 2048 frames (42.667 ms), con salto de 1024 frames (21.333 ms).
- Busca el pico de mayor potencia dentro de `[f_min_hz, f_max_hz]`; estima frecuencia con interpolación parabólica del logaritmo de potencia.
- Calcula el piso local como la mediana de potencia por bin dentro de la banda, excluyendo 100 Hz alrededor del pico. `confidence_db` es el exceso del pico sobre ese piso, no una probabilidad calibrada.
- Emite presencia por canal, frecuencia estimada, nivel RMS en dBFS, piso local, confianza en dB y contadores de estabilidad.
- Requiere por defecto dos ventanas estables para afirmar presencia y dos fallos para retirarla. El emparejamiento de ventanas consecutivas usa 75 Hz como tolerancia provisional.
- Mantiene un único búfer de análisis de tamaño fijo; rechaza bloques mayores de 4096 frames. No crea cola de paquetes. La aplicación en lazo continuo debe seguir midiendo tiempo de CPU y antigüedad de cola contra el presupuesto acordado de 70 ms p95 por bloque.

Los valores de 15 dB de prominencia y −120 dBFS de nivel mínimo son parámetros iniciales del código, no criterios aprobados. El umbral pasó de 12 a 15 dB en la caracterización sintética de 8.3 porque 12 dB produjo indicaciones falsas con ruido coloreado y un tono cercano al borde de banda. Antes de la campaña física deben acordarse SNR mínimo de trabajo, tasa mínima de detección, tasa máxima de falsos positivos y error de frecuencia. La ventana de 2048 frames es candidata: fue la menor de las longitudes ensayadas que evitó falsas detecciones en esa matriz, pero aún no se ha demostrado que sea la ventana más corta que satisfaga los criterios finales.

## Entrega y estado

La clase `StreamingSingleToneDetector` y `ToneDetectorConfig` están en `pynq_acoustic_camera.single_tone`; se exportan de forma diferida desde el paquete para evitar importar NumPy al importar el paquete base. La versión de desarrollo `0.1.0rc3` se instaló en la PYNQ y se verificó en el bucle PCM continuo en [evidencia 8.9](../../evidence/phase-08-step-09/README.md). La aceptación acústica sigue pendiente: el contraste operativo no está fijado, falta la campaña independiente con voz/ambiente y la latencia física requiere sincronización.

## Actualización 8.8 — evento del sistema, provisional

La evidencia física 8.7 mostró muchas rachas positivas por canal durante conversación humana sin tono. Se añadió `SingleToneEventTracker` al detector incremental. Por ventana, busca al menos tres de los cuatro candidatos con frecuencias separadas hasta 6 Hz respecto de uno de ellos. Requiere cuatro ventanas consecutivas con medianas dentro de un intervalo total de 6 Hz para iniciar un único evento del sistema. Dos ventanas sin apoyo en torno a la frecuencia activa lo finalizan. La memoria de esta decisión está acotada a cuatro observaciones y contadores; no se retienen bloques PCM. La tolerancia inicial de 12 Hz produjo un evento falso de 234 ms durante una toma nueva de 35 s con conversación; la versión de 6 Hz se comprobó después en tomas físicas de desarrollo.

Cada resultado incluye `system.present`, `system.frequency_hz`, canales de apoyo y un `event` opcional de tipo `start` o `end`. `reported_frame` es el final de la ventana que produjo la decisión; `start_frame` es el final de la primera ventana que apoyó el inicio. Ninguno equivale al comienzo acústico real. La marca de latencia física requiere PCM bruto conservado y sincronización independiente.

El replay inicial de desarrollo sobre las ventanas guardadas en 8.7, que también se usaron para escoger la primera regla de 12 Hz, está en `evidence/phase-08-step-08/development-replay-8_7-speech.json`. En unos 35 s de intervalos conservadores de voz sola hubo seis eventos breves y ninguno de al menos 200 ms (máximo 128 ms); en las zonas estables aproximadas de los siete niveles, la presencia del sistema fue 97,9–100 % de las ventanas. La primera toma nueva de voz sola sí produjo un evento falso de 234 ms, y motivó la tolerancia de 6 Hz.

La versión actual congela la frecuencia al iniciar el evento y sigue la energía de esa frecuencia aunque otro pico de voz sea más fuerte. El seguimiento compara el máximo de tres bins alrededor de la frecuencia bloqueada con la mediana de bins a 80–300 Hz de distancia dentro de la banda; exige 12 dB en tres canales para sostener la presencia. El PCM bruto de una toma de desarrollo con dos tonos débiles permitió elegir este umbral. Ni esa misma toma ni los replays son aceptación independiente. La escritura de PCM bruto en el lazo produjo una edad de cola p95 de 210 ms y queda fuera de la configuración de producto; las ejecuciones normales sin ese diagnóstico deben medirse separadamente.

En la toma física final de 8.8 con conversación humana y tres niveles de 1877 Hz, se desactivaron las FFT y trazas diagnósticas del runner. La vecindad de seguimiento se reutiliza durante el evento y el cálculo de sus cuatro canales está vectorizado. Captura + detector usaron 65,04 ms p95 por bloque de 4096 tramas, con cero pérdidas y sin crecimiento de cola. Los tres niveles dieron eventos cerca de 1877 Hz; el nivel −36 dB se interrumpió unos 128 ms y volvió a detectarse. Esta caracterización de desarrollo no certifica la latencia acústica ni el contraste mínimo; consultar `evidence/phase-08-step-08/README.md` antes de aceptación independiente.
