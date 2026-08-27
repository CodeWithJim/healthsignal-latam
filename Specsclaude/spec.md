# Especificación — HealthSignal LATAM

| | |
|---|---|
| **Versión** | 0.2 |
| **Fecha** | 2026-08-26 |
| **Estado** | Borrador para revisión |
| **Reemplaza** | `spec.txt` v0.1 |
| **Gobernado por** | [`constitution.md`](constitution.md) v1.0 |
| **Implementación** | [`plan.md`](plan.md) v0.2 |

Este documento describe **qué** debe hacer el sistema y **por qué**. No prescribe tecnología,
librerías ni estructura de código: eso vive en `plan.md`. Todo requisito aquí debe poder
convertirse en un test que falle.

Las cifras marcadas *(medido)* fueron verificadas sobre RISA Data V1.0 Candidate 1 el 2026-08-26,
con los 17 archivos coincidiendo byte a byte contra `MANIFEST_SHA256.txt`.

---

## 1. Problema y propósito

RISA es una red de salud sintética que observa a 1.000 pacientes desde fuentes que no comparten
frecuencia, estructura, unidad ni instante de captura. El reto no es la falta de datos: es
convertir 2.552.104 registros heterogéneos en **señales de riesgo oportunas, priorizadas,
explicables y trazables**, sin convertir cada variación en una alerta.

El sistema responde siete preguntas sobre cada situación que prioriza: *qué detectó, para quién,
desde cuándo era razonable saberlo, qué tan prioritario es, por qué, con qué evidencia, y si esa
evidencia estaba disponible en ese momento.*

## 2. Alcance

**Dentro:** ingesta y homogeneización de las 17 fuentes; alineación temporal respetando
disponibilidad; detección de patrones multivariables; priorización; control de alertas
irrelevantes; generación de evidencia trazable; explicación; exploración interactiva de resultados.

**Fuera:** diagnóstico clínico, pronóstico de enfermedad, recomendación terapéutica, cálculo de
dosis, y toda afirmación causal sobre el estado del paciente (P-01).

## 3. Actores

| Actor | Necesidad |
|---|---|
| **Evaluador técnico** | Auditar una señal cualquiera hasta sus filas fuente; solicitar una situación no preparada y obtener respuesta en vivo |
| **Revisor clínico** (simulado) | Saber a quién revisar primero y por qué, con la evolución visible |
| **Operador de la red** | Distinguir un riesgo fisiológico de un problema de conectividad o de dispositivo |

---

## 4. Requisitos de datos

Hechos verificados sobre el dataset que el sistema debe honrar. Cada uno es comprobable con una
consulta.

### RD-01 · Ventana de estudio
Todo el monitoreo ocurre entre **2026-07-01 00:00:00 y 2026-07-31 08:00:00** *(medido)*. Ninguna
decisión se declara fuera de ese rango.

### RD-02 · Contrato temporal por fuente
El sistema asigna a cada registro un tiempo de evento y uno de disponibilidad según esta tabla.
Las seis primeras filas son la tabla oficial; las tres últimas son **decisión documentada del
equipo** bajo la cláusula *"salvo otra lógica documentada"*.

| Fuente | Tiempo de evento | Tiempo de disponibilidad | Latencia *(medida)* |
|---|---|---|---|
| `vital_signs` (MONITOR_GATEWAY) | `timestamp` | `timestamp` | 0 |
| `wearable_observations` | `timestamp` | `sync_datetime` | 0–30 min · p50 4 |
| `laboratory_results` | `sample_datetime` | `result_datetime` | 30–360 min · p50 133 |
| `conditions` | `onset_date` | `recorded_datetime` | todas anteriores a 2026-07-01 |
| `patient_context` | `start_datetime`/`end_datetime` | `start_datetime` | intervalo |
| `connectivity_events` | `start_datetime`/`end_datetime` | `start_datetime` | intervalo |
| `device_observations` | `timestamp` | `timestamp` | fuente MONITOR_GATEWAY, latencia declarada NEAR_REAL_TIME |
| `medication_administrations` | `start_datetime` | `start_datetime` | fuente EHR_MED, latencia declarada LOW |
| `encounters` | `start_datetime` | `start_datetime` | fuente EHR_CORE, latencia declarada LOW |

**Verificable:** ninguna fila de la capa homogeneizada tiene `available_time < event_time`.

### RD-03 · Preservación de identificadores
Toda fila de la capa homogeneizada conserva `source_file` y el `record_id` de su tabla de origen
(`observation_id`, `wearable_observation_id`, `lab_result_id`, `device_observation_id`,
`condition_id`, `context_id`, `event_id`, `administration_id`, `encounter_id`), más los
identificadores de integración `patient_id`, `encounter_id`, `device_id` y `facility_id` cuando la
fuente los provea.

**Verificable:** cada `record_id` citado en la salida existe en su archivo original.

### RD-04 · Normalización de unidades
Toda magnitud se convierte a su unidad canónica usando `units_catalog.csv`
(`valor_canónico = valor × factor + offset`) antes de cualquier comparación. Se conserva el valor y
la unidad originales para auditar la conversión.

*(medido)* 166 filas de `TEMP` vienen en `degF`. Sin convertir, el máximo de `TEMP` es 99,45 —
166 fiebres aparentes de 98 °C. Con la conversión del catálogo, cero valores implausibles de `TEMP`.

**Verificable:** tras la conversión, `TEMP` no supera `plausibility_max` en ninguna fila.

### RD-05 · Deduplicación de retransmisiones
*(medido)* 540 filas con `source_system = MONITOR_RETRANSMIT` duplican exactamente —mismo valor—
una fila existente de `MONITOR_GATEWAY` con la misma llave `(patient_id, variable_code, timestamp)`.
Afectan a 45 pacientes, 12 filas cada uno, y las 540 caen dentro de una ventana de
`connectivity_events` del mismo paciente.

El sistema conserva la fila de `MONITOR_GATEWAY` y marca la retransmitida como duplicada. La
retransmisión **no se descarta**: se preserva como evidencia de calidad citable (P-04).

**Verificable:** ninguna llave `(patient_id, variable_code, event_time)` aparece más de una vez
entre las filas aceptadas.

### RD-06 · Gate de plausibilidad propio
El sistema valida cada valor numérico contra `plausibility_min`/`plausibility_max` de
`variable_catalog.csv` y **no delega esa validación en `quality_flag`**.

*(medido)* 549 valores de `SpO2` superiores a 100 % vienen marcados `OK`. En sentido contrario, las
4.164 filas marcadas `CHECK` contienen mayormente valores normales (HR 83,5 · RR 21,4 · SpO2 99,1).
Filtrar por `quality_flag != OK` descarta datos buenos y conserva imposibles.

Totales implausibles *(medido)*: `SpO2` 734 · `RR` 22 · `SBP` 4 · `HR` 1 · `DBP` 1.

**Verificable:** las 762 filas implausibles quedan marcadas `is_plausible = false` y ninguna
contribuye al cálculo de un score. **No se descartan**: permanecen en la tabla para seguir siendo
citables como evidencia `QUALITY` con su `record_id` real (P-04). La cuarentena queda reservada
para filas que no pudieron interpretarse —fecha ilegible, unidad desconocida, identificador
ausente—, de las cuales *(medido)* Candidate 1 no contiene ninguna.

### RD-07 · Unión de conectividad por paciente, no por dispositivo
*(medido)* `connectivity_events.device_id` es 100 % wearables (prefijo `WRB-`), mientras los signos
vitales usan dispositivos `DEV-`. Unir por `device_id` contra `vital_signs` devuelve cero filas.
La unión válida es `patient_id` más contención temporal.

**Verificable:** las 540 filas retransmitidas se asocian, cada una, a un evento de conectividad.

### RD-08 · Grilla de muestreo y cobertura exacta
*(medido)* El muestreo es regular: `HR`, `RR`, `SpO2` cada 20 min; `TEMP` cada 60 min; `SBP`, `DBP`
cada 120 min (p05 = p50 = p95 en los tres casos). No hay celdas vacías en ninguno de los 17
archivos. Los huecos reales en `HR` son 75 en total sobre 1.000 pacientes, con máximo de 80 min.

Por lo tanto la cobertura de una ventana es un **conteo exacto** contra el número esperado de
muestras, no una estimación.

**Verificable:** la cobertura reportada en una ventana coincide con
`muestras_presentes / muestras_esperadas` según la grilla nominal.

### RD-09 · Cobertura por encuentro
*(medido)* Cada paciente tiene exactamente un encuentro, de 2,2 a 26,4 días (p50 6). El 100 % de
las observaciones de signos vitales cae dentro de la ventana de su encuentro. Fuera de ella no hay
ausencia de dato: no hay monitoreo.

El sistema **no evalúa** instantes fuera del encuentro del paciente.

---

## 5. Requisitos funcionales

### RF-01 · Ingesta declarativa y recurrente
Cada fuente se incorpora mediante una declaración que especifica: archivo, columna de `record_id`,
columna de tiempo de evento, regla de tiempo de disponibilidad, mapeo de variables, columna de
unidad, y si la fuente es releíble.

**Criterio de aceptación:** agregar una fuente nueva no requiere modificar el código del pipeline,
sólo añadir una declaración. Se demuestra incorporando una fuente sintética adicional durante la
evaluación.

### RF-02 · Clasificación explícita, sin descartes silenciosos
La homogeneización clasifica cada fila leída como aceptada o en cuarentena, con motivo.

**Criterio de aceptación:** `filas_leídas = aceptadas + cuarentena` para cada una de las 17 fuentes,
verificado por consulta. El motivo de cuarentena es consultable por `record_id`.

### RF-03 · Lectura exclusivamente as-of
El componente que produce dictámenes accede a los datos únicamente mediante una operación que
recibe `(patient_id, T)` y devuelve solamente hechos con `available_time ≤ T`.

**Criterio de aceptación:** un doble de prueba que registre toda lectura demuestra que ninguna
consulta del motor solicita ni recibe filas posteriores a `T`, incluidas las del cálculo de baseline.

### RF-04 · Baseline individual anterior a la evidencia
Para cada canal fisiológico, el sistema estima un nivel y una dispersión de referencia del propio
paciente sobre una ventana que **termina donde empieza la ventana de evidencia** y nunca se solapa
con ella.

**Criterio de aceptación:** dado un paciente con un evento contenido íntegramente en la ventana de
evidencia, el baseline calculado es idéntico al que se obtiene eliminando ese evento del dataset.

### RF-05 · Canal sin baseline no participa
Si un canal no alcanza el mínimo de muestras de baseline, queda excluido del dictamen. No se le
asigna valor por defecto, ni se lo trata como normal.

**Criterio de aceptación:** un paciente sin `SBP` suficiente produce un dictamen cuyas
contribuciones no incluyen `SBP`, y cuya explicación no lo menciona como estable.

### RF-06 · Detección por concordancia multivariable
La señal se construye a partir de la **coincidencia direccional sostenida entre canales**
—`HR`↑ `RR`↑ `TEMP`↑ `SpO2`↓— medida contra baseline individual, y no a partir de umbrales
absolutos sobre valores individuales.

**Criterio de aceptación (decisivo):** con la trayectoria del documento oficial
(08:00 HR 88 SpO2 95 RR 18 T 37,1 → 11:00 HR 108 SpO2 91 RR 25 T 38,0), el sistema emite una señal
de prioridad `HIGH` o superior **con decisión declarada entre las 09:00 y las 10:00**, es decir
antes de que cualquier lectura individual cruce un umbral convencional.

**Criterio de aceptación (recíproco):** un único canal desviado de forma extrema produce un score
estrictamente menor que cuatro canales desviados moderadamente en dirección concordante.

### RF-07 · Corroboración multifuente
Cuando hay resultados de laboratorio disponibles en `T` fuera de su rango de referencia, o
coincidencia entre la frecuencia cardíaca del monitor y la del wearable, la señal incorpora esa
corroboración y la cita como evidencia.

**Criterio de aceptación:** al menos una señal de la entrega final se sustenta en tres fuentes
distintas, con evidencia de cada una.

### RF-08 · Supresión con evidencia citada
El sistema reduce la prioridad de una señal cuando existe evidencia disponible que explica la
variación. Cada supresión que se activa **emite su propia fila de evidencia** con el `record_id`
que la justifica.

Situaciones que deben suprimirse, con su marcador *(medido)*:

| Situación | Marcador | Volumen |
|---|---|---|
| Variación explicada por actividad física | `patient_context` PHYSICAL_ACTIVITY HIGH/MODERATE | 478 / 978 |
| Recuperación posterior a actividad | `patient_context` RECOVERY_PHASE | 70 |
| Caída aislada por calidad de señal | `quality_flag` LOW_SIGNAL (`SpO2` 69–75 %) | 108 |
| Valor imposible aislado | Fuera de plausibilidad | 762 |
| Ausencia por conectividad | `connectivity_events` | 434 |

**Criterio de aceptación:** ninguna señal de prioridad `HIGH` o superior en la entrega final tiene
su ventana de evidencia dominada por uno de estos marcadores sin una supresión registrada.

### RF-09 · Riesgo y confianza como ejes independientes
`risk_score` mide relevancia de la señal. `confidence_score` mide cuánto sustento tiene el dictamen
—cobertura, calidad y antigüedad del dato. **La ausencia de datos reduce la confianza y nunca
incrementa el riesgo** (P-06).

**Criterio de aceptación:** dos ventanas idénticas donde una tiene 40 % menos de cobertura producen
el mismo `risk_score` y distinto `confidence_score`.

### RF-10 · Compuertas duras de prioridad
`CRITICAL` exige concordancia de al menos tres canales, persistencia sostenida, cobertura suficiente
y ausencia de supresión activa. Ninguna combinación de score alcanza `CRITICAL` sin cumplirlas.

**Criterio de aceptación:** ninguna señal `CRITICAL` de la entrega final proviene de un solo canal,
ni de una ventana con cobertura insuficiente, ni tiene supresión activa.

### RF-11 · Política de eventización documentada
El sistema evalúa en una cadencia regular pero **emite una señal sólo ante un cambio material de
estado**: primer ingreso a una banda de prioridad, o escalamiento. La política —cadencia, condición
de emisión, período refractario— está documentada.

**Criterio de aceptación:** el volumen de `signals.csv` es del orden de 10²–10³ filas, no de 10⁵.
La política se puede enunciar en tres líneas y reproducir el mismo conjunto de señales.

### RF-12 · Explicación determinista y verificable
Cada señal incluye una explicación breve que indica qué cambió, en qué canales, durante cuánto
tiempo, con qué corroboración y con qué supresiones evaluadas. Se genera desde los mismos objetos
que produjeron el score.

**Criterio de aceptación:** la explicación es reproducible byte a byte entre corridas, y cada
afirmación cuantitativa que contiene se puede verificar contra las filas de evidencia de esa señal.

### RF-13 · Priorización comparable entre pacientes
El sistema ordena las situaciones identificadas y permite justificar por qué una precede a otra.

**Criterio de aceptación:** dadas dos señales cualesquiera, el sistema expone las contribuciones por
canal, la corroboración y las supresiones de ambas, de modo que la diferencia de orden sea
explicable sin recurrir al valor del score.

### RF-14 · Exploración y decisión en vivo
Un mecanismo funcional permite consultar cualquier paciente, su evolución temporal, sus señales y
la evidencia de cada una; y **solicitar una decisión en un instante arbitrario elegido por el
evaluador**, computada en el momento con la garantía de RF-03.

**Criterio de aceptación:** el evaluador nombra un paciente y una fecha-hora no preparados de
antemano; el sistema responde con score, prioridad, explicación, supresiones activas y las filas
fuente exactas utilizadas.

### RF-15 · Auditoría de una señal hasta el archivo original
Desde cualquier señal se puede llegar a las filas de los CSV originales que la sustentan.

**Criterio de aceptación:** para una señal elegida al azar, se muestran las filas originales
correspondientes a cada `record_id` citado.

---

## 6. Requisitos de salida

### RS-01 · Archivos y columnas
El sistema produce `results/signals.csv` y `results/evidence.csv` con las columnas requeridas por
`validate_submission.py`. Se emiten además las opcionales `confidence_score`, `variable_code` y
`contribution`. Columnas adicionales están permitidas *(verificado en el validador: sólo comprueba
presencia de las requeridas)*.

### RS-02 · Restricciones de contenido
`risk_score` y `confidence_score` numéricos en [0, 1]. `priority_level` en
{`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`}. `evidence_role` en
{`PRIMARY`, `SUPPORTING`, `CONTEXT`, `QUALITY`}. `signal_id` único y no vacío. `explanation` y
`model_version` no vacíos.

### RS-03 · Restricciones temporales
Por señal: `evidence_start ≤ evidence_end ≤ decision_datetime`.
Por fila de evidencia: `available_datetime ≤ decision_datetime` de su señal.

### RS-04 · Integridad relacional bidireccional
Toda señal tiene al menos una fila de evidencia. Toda fila de evidencia referencia un `signal_id`
existente.

*El validador rechaza ambos casos: señales sin evidencia (línea 155) y evidencia con `signal_id`
desconocido (línea 122).*

### RS-05 · Formato de fecha uniforme
Todas las fechas-hora de salida se emiten **sin zona horaria**, en formato ISO 8601, consistente
con los datos de origen *(medido: los 17 archivos usan timestamps sin offset)*.

*Mezclar fechas con y sin zona horaria hace que `validate_submission.py` termine con
`TypeError: can't compare offset-naive and offset-aware datetimes` —una excepción no capturada por
su `except ValueError`— produciendo un traceback en lugar de un `[FAIL]` legible. Verificado
ejecutando el código del validador.*

### RS-06 · Correspondencia con la ejecución final
`signals.csv` y `evidence.csv` provienen de la misma corrida, identificada por `model_version`.

---

## 7. Requisitos no funcionales

### RNF-01 · Reproducibilidad
Dos ejecuciones sobre la misma entrada producen archivos idénticos byte a byte (P-05).

### RNF-02 · Tiempo de respuesta interactivo
Una decisión individual solicitada en vivo (RF-14) responde en menos de 3 segundos.

### RNF-03 · Tiempo de proceso completo
La corrida completa termina en menos de 15 minutos en una máquina de escritorio.
*Referencia medida: un barrido completo de los 240 MB tomó 30,6 s en Python sin librerías de
dataframes, y una sonda de detección sobre los 1.000 pacientes, 90,6 s.*

### RNF-04 · Verificación de integridad de origen
Cada corrida verifica el SHA-256 de los 17 archivos contra el manifiesto y se detiene ante una
discrepancia (P-08).

### RNF-05 · Calibración externalizada
Toda constante de calibración vive en configuración versionada.
*El `README.md` del dataset declara `RISA Data V1.0 Candidate 1 — not yet frozen final release`.
Si llega una versión congelada distinta, recalibrar debe ser ejecutar un script, no reescribir el
motor.*

### RNF-06 · Manejo de credenciales y exposición
Ninguna clave ni token en el código. La solución opera exclusivamente sobre los datos suministrados.
El acceso al mecanismo de consulta está separado del acceso a los datos crudos.

---

## 8. Criterios de éxito

Cada uno es medible y se reporta en la entrega.

| ID | Criterio | Objetivo |
|---|---|---|
| **CE-01** | Causalidad temporal: filas de evidencia con `available_datetime ≤ decision_datetime` | **100 %** |
| **CE-02** | Cobertura de evidencia: señales con al menos una fila trazable | **100 %** |
| **CE-03** | `validate_submission.py` sobre la entrega final | **0 errores** |
| **CE-04** | Impacto en distractores: señales `HIGH`+ cuya ventana está dominada por un marcador conocido (RF-08) sin supresión registrada | **0** |
| **CE-05** | Detección temprana: anticipación mediana respecto del punto de máxima desviación, en los casos de deterioro sostenido | **> 2 h** |
| **CE-06** | Reducción de falsas alertas por ablación: señales `HIGH`+ del motor completo frente a la configuración de sólo umbrales | **reducción reportada con cifras** |
| **CE-07** | Integración multifuente: señales sustentadas en ≥ 3 fuentes distintas | **≥ 1**, exhibible |
| **CE-08** | Volumen de salida | **10²–10³ señales** |

**Nota sobre CE-04 y CE-05:** no existe Gold Standard público. El conjunto de negativos se
construye desde los marcadores públicos de RF-08 y la ventana de oportunidad de CE-05 la define el
equipo; ambas definiciones se explican en el README, conforme autoriza la guía técnica.

---

## 9. Escenarios de aceptación

Se implementan como pruebas sobre trayectorias sintéticas, sin leer los CSV. Son la condición de
entrada del motor a la etapa de datos reales.

| ID | Dado | Cuando | Entonces |
|---|---|---|---|
| **CA-01** | La trayectoria oficial 08:00→11:00 | Se evalúa a las 09:00 y a las 10:00 | Emite `HIGH` o superior con 3+ canales concordantes citados |
| **CA-02** | HR sube 40 bpm con `ACTIVITY_LEVEL = HIGH` y retorna a baseline en 40 min | Se evalúa durante y después del pico | No emite `HIGH`+; registra supresión por actividad citando el `context_id` |
| **CA-03** | `SpO2` cae a 71 % en una muestra y vuelve a 96 % en la siguiente | Se evalúa en el instante de la caída | No emite; registra supresión por calidad citando el `observation_id` |
| **CA-04** | Paciente estable con 4 h sin observaciones durante un evento de conectividad | Se evalúa al reanudarse | No emite `HIGH`+; `confidence_score` reducido; cita el `event_id` como `QUALITY` |
| **CA-05** | Fluctuación aleatoria dentro de ±1 MAD en los cuatro canales | Se evalúa en toda la ventana | No emite señal |
| **CA-06** | Un canal a +10 desviaciones, tres canales estables | Se evalúa | Score estrictamente menor que el de CA-01; nunca `CRITICAL` |
| **CA-07** | Cualquier señal emitida | Se inspecciona su evidencia | Todo `available_datetime` ≤ `decision_datetime` y todo `record_id` existe en su archivo |

---

## 10. Casos borde

| Situación | Comportamiento esperado |
|---|---|
| Paciente con baseline insuficiente en todos los canales | No se emite señal. No se asume normalidad (RF-05) |
| Instante de decisión anterior al inicio del encuentro | No se evalúa (RD-09) |
| Ventana de evidencia enteramente dentro de una desconexión | Cobertura 0; no se emite; el hecho queda registrado |
| Laboratorio tomado antes de `T` pero informado después | No se usa. Verificable: latencia mediana 133 min crea esta situación con frecuencia |
| Retransmisión sin fila original de gateway | Se acepta como observación válida. *No ocurre en Candidate 1 (540 de 540 tienen original), pero la regla debe estar definida* |
| Valor categórico donde se espera numérico | A cuarentena con motivo. Ocurre en `ACTIVITY_LEVEL` dentro de `wearable_observations` |
| Dos señales del mismo paciente en instantes contiguos | La política de eventización decide si es una señal o dos (RF-11) |

---

## 11. Pendientes de definición

Marcadores `[NEEDS CLARIFICATION]` que deben resolverse antes de cerrar la versión 1.0. Ninguno
bloquea el inicio de la implementación.

| ID | Pendiente | Cómo se resuelve |
|---|---|---|
| **NC-01** | Cadencia exacta de evaluación | Propuesta: horaria dentro del encuentro (≈144.000 evaluaciones). Se confirma midiendo el tiempo real de corrida contra RNF-03 |
| **NC-02** | Constantes de calibración: techo por canal, multiplicadores de concordancia, `k₀`, bandas de prioridad, pisos de dispersión | Se fijan tras la primera corrida completa, contra la distribución observada. *Referencia de la sonda: mediana 4,28 · p95 16,80 · máximo ≈ 27,3* |
| **NC-03** | Volumen objetivo dentro del rango de CE-08 | Decisión del equipo tras ver la primera distribución de scores |
| **NC-04** | Ventana de oportunidad para CE-05 | Definirla y justificarla en el README |
| **NC-05** | Mínimo de muestras de baseline por canal | Derivar de la grilla nominal de RD-08 y de la distribución de cobertura |
| **NC-06** | Superficie del mecanismo de exploración de RF-14 | Decisión del equipo. Ver `plan.md` |

---

## 12. Fuera de alcance

- Diagnóstico, pronóstico, prescripción o cálculo de dosis (P-01).
- Entrenamiento de modelos supervisados: no existen etiquetas públicas.
- Procesamiento en streaming o arquitectura distribuida: el dataset es estático y congelado.
- Modelos generativos en la ruta de cálculo de score, prioridad o selección de evidencia (P-09).
- Integración con sistemas externos o estándares de interoperabilidad no requeridos por el reto.
