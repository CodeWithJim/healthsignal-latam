# Constitución — HealthSignal LATAM

| | |
|---|---|
| **Versión** | 1.0 |
| **Fecha** | 2026-08-26 |
| **Estado** | Vigente |
| **Ámbito** | Todas las decisiones técnicas del prototipo, sin excepción |

Los principios de este documento **gobiernan** el spec y el plan. Ante un conflicto entre un
requisito y un principio, el principio manda y el requisito se reescribe. Ningún atajo de
implementación, presión de tiempo ni mejora de puntaje justifica violar uno.

---

## P-01 · Apoyo a la decisión, nunca diagnóstico

El sistema detecta, prioriza y explica **señales que ameritan revisión profesional**. No emite
diagnósticos, no prescribe, no decide conductas clínicas.

**Por qué:** el alcance oficial lo delimita explícitamente, y la regla 7 de evaluación establece que
presentar resultados como diagnósticos o decisiones clínicas autónomas **no otorga mayor valoración**.

**Cómo se verifica:** ningún texto de salida —`explanation`, UI, API, README, pitch— afirma una
condición clínica, una causa o una conducta. Se revisa el vocabulario de las plantillas de
explicación antes de cada entrega.

---

## P-02 · Causalidad temporal absoluta

Para una decisión declarada en `T`, toda evidencia utilizada cumple `available_time ≤ T`.
Sin excepciones, sin casos especiales, sin "esto no cuenta porque es contexto".

**Por qué:** es la regla de oro del escenario. Una solución retrospectivamente correcta que use
información no disponible es **inválida como anticipación**, no meramente imprecisa.

**Cómo se verifica:** por construcción, no por revisión. El núcleo de decisión lee exclusivamente a
través de un puerto que exige `T` y nunca devuelve hechos posteriores. La base de datos declara
`CHECK (available_time >= event_time)`. Un test independiente recorre la salida completa y falla si
una sola fila de evidencia tiene `available_datetime > decision_datetime`.

Incluye las formas menos evidentes: **baselines que incluyen la ventana de evidencia**, agregados
que cruzan el instante de decisión, y cualquier estadístico poblacional calculado sobre el período
completo.

---

## P-03 · Toda señal regresa a sus registros fuente

Cada señal emitida está ligada a al menos un registro real de RISA, identificado por
`source_file` + `record_id` existentes en los archivos originales.

**Por qué:** *"un `risk_score` de 0.92 ayuda a ordenar, pero no constituye evidencia por sí mismo"*.
Una alerta cuya procedencia no puede identificarse tiene menor valoración en cuatro de los cinco
criterios específicos del reto.

**Cómo se verifica:** la tabla de evidencia declara clave foránea contra la de señales, y un test
comprueba que cada `record_id` citado existe en su archivo de origen. La evidencia se **genera
desde el mismo cómputo que produjo el score**; nunca se reconstruye a posteriori.

---

## P-04 · Nada silencioso

Toda transformación, exclusión, imputación, deduplicación o supresión queda registrada, es
consultable y es citable como evidencia.

**Por qué:** los documentos exigen que el tratamiento de calidad y de datos faltantes esté
justificado. Una decisión de preparación que no deja rastro no se puede defender ante un evaluador.

**Cómo se aplica:** toda fila leída termina con un veredicto explícito. Se cumple en todo momento
`filas_leídas = filas_aceptadas + filas_en_cuarentena`, y esa igualdad se verifica con una consulta.
Cada regla de supresión que reduce un score emite su propia fila de evidencia con el `record_id`
que la justifica.

---

## P-05 · Determinismo y reproducibilidad

La misma entrada produce la misma salida, byte a byte. Toda constante de calibración vive en
configuración versionada, nunca en el código.

**Por qué:** `model_version` es una columna obligatoria de salida y sólo tiene sentido si identifica
un artefacto reproducible. Un evaluador debe poder pedir la misma decisión dos veces y obtener la
misma respuesta.

**Cómo se verifica:** dos corridas consecutivas sobre la misma entrada producen archivos idénticos.
Ningún componente no determinista participa en el cálculo de `risk_score`, `priority_level`,
`confidence_score` ni en la selección de evidencia.

---

## P-06 · La ausencia de datos no es evidencia de nada

Un dato faltante no equivale a cero, ni a normalidad, ni a riesgo.

**Por qué:** está enunciado en los tres documentos oficiales, y es el error que la guía lista como
*"tratar missing como cero/normal sin justificación"*.

**Cómo se aplica:** la falta de datos **reduce la confianza y nunca incrementa el riesgo**.
`risk_score` y `confidence_score` son ejes independientes. Un canal sin baseline suficiente no
participa del dictamen: no se le asigna un valor por defecto. Riesgo alto con confianza baja se
comunica como **solicitud de verificación**, no como alerta clínica.

---

## P-07 · Un valor extremo no es una señal

Ningún valor aislado, por extremo que sea, alcanza por sí solo la prioridad máxima.

**Por qué:** *"confundir un valor extremo con una señal prioritaria"* está listado entre los errores
que reducen el valor de una propuesta. La tesis del reto es que el cambio moderado y simultáneo en
varias variables importa más que un único extremo.

**Cómo se aplica:** la contribución de cada canal se recorta antes de agregarse, y `CRITICAL` exige
concordancia de al menos tres canales más persistencia. La compuerta es estructural, no un umbral
ajustable.

---

## P-08 · Los originales no se sobrescriben

Los 17 CSV de RISA Data V1.0 son de sólo lectura. Todas las representaciones derivadas viven en
capas separadas.

**Por qué:** requisito explícito de implementación, y base de la auditabilidad: la fuente de verdad
debe poder compararse contra cualquier resultado.

**Cómo se verifica:** el hash SHA-256 de cada archivo original se registra en el manifiesto de
ingesta y se compara en cada corrida. Una discrepancia detiene el proceso.

---

## P-09 · Evidencia extraída ≠ contenido generado

Cualquier contenido producido por un modelo generativo se mantiene identificable y separado de la
evidencia extraída de los datos.

**Por qué:** los documentos lo exigen textualmente, y la demostración debe permitir diferenciar los
resultados sustentados en datos de cualquier contenido generado adicionalmente.

**Cómo se aplica:** ningún componente generativo participa en el cálculo de scores, en la asignación
de prioridad ni en la selección de evidencia. Si se agrega una capa narrativa, viaja en campos
distintos, marcados como generados, y su ausencia no altera ninguna otra salida.

---

## Declaración de uso de herramientas generativas

El desarrollo utiliza Claude Code (Anthropic) como asistente de programación y análisis. Su
participación se declara en el README técnico conforme al requisito de transparencia. Ningún
modelo generativo interviene en la ruta de decisión ni de evidencia (P-09).
