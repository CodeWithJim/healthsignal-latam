# HealthSignal LATAM — Motor de Concordancia

Sistema de apoyo a la decisión que convierte los 2.552.104 registros heterogéneos de la red
sintética RISA en señales de riesgo priorizadas, explicables y trazables hasta su fila de origen.

No emite diagnósticos ni prescripciones: detecta y prioriza situaciones que ameritan revisión
profesional.

**Tesis:** no se detectan valores altos. Se detectan **varios canales fisiológicos moviéndose
juntos, en la dirección clínicamente coherente, de forma sostenida**, medidos contra el propio
historial del paciente y usando exclusivamente información disponible en el instante de la decisión.

## Documentos

| Documento | Contenido |
|---|---|
| [`Specsclaude/constitution.md`](Specsclaude/constitution.md) | Nueve principios innegociables que gobiernan todas las decisiones |
| [`Specsclaude/spec.md`](Specsclaude/spec.md) | Qué debe hacer el sistema y por qué, en requisitos verificables |
| [`Specsclaude/plan.md`](Specsclaude/plan.md) | Cómo se construye: arquitectura, esquema, algoritmo, fases |

## Instalación

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Los 17 CSV de RISA Data V1.0 van en `data/raw/`, conservando la estructura de carpetas del paquete
original (`01_master/`, `02_clinical/`, `03_monitoring/`, `04_context/`, `05_metadata/`). No se
versionan: pesan 244 MB y su integridad se verifica contra `MANIFEST_SHA256.txt` en cada corrida.

## Ejecución

```bash
# Etapas 0-2 · RAW -> CLEAN   (~3 min, produce data/warehouse.duckdb)
.venv\Scripts\python.exe scripts\00_ingest.py

# Etapa 3 · un paciente recorrido a través del puerto as-of
.venv\Scripts\python.exe scripts\01_snapshot.py PAT-0869 2026-07-20T18:00:00

# Criterios de aceptación
.venv\Scripts\python.exe -m pytest tests -q
```

## Arquitectura

```
data/raw/          los 17 CSV originales · sólo lectura · nunca se escriben
       ↓  ingest        adaptadores declarativos en config/sources.yaml
capa CLEAN         unidades canónicas · deduplicación · plausibilidad · available_time
       ↓  features
capa FEATURES      baseline por paciente/canal · cobertura
       ↓
   AsOfStore.snapshot(patient_id, T)   ← única vía de lectura del núcleo
       ↓
núcleo de dominio  funciones puras · sin IO · concordancia y supresión
       ↓
capa RESULTS       signals.csv + evidence.csv
```

**La regla temporal es una restricción de base de datos, no una convención.** La tabla
`observations` declara `CHECK (available_time >= event_time)`, y la tabla `evidence` declara clave
foránea contra `signals`: evidencia huérfana es imposible de insertar. Ambas cosas están cubiertas
por pruebas que intentan violarlas.

## Estado

| Fase | Estado |
|---|---|
| **0 · RAW → CLEAN** | Completa. 17/17 hashes verificados, 2.549.046 filas clasificadas |
| **1 · AsOfStore** | Completa. Puerto as-of, objetos de dominio y 29 pruebas en verde |
| 2 · Motor de concordancia | Pendiente |
| 3 · Supresión, métricas, interfaz | Pendiente |
| 4 · Decisión en vivo y entregables | Pendiente |

## Hallazgos que condicionan el diseño

Verificados sobre RISA Data V1.0 Candidate 1 el 2026-08-26.

- **`quality_flag` no sirve como filtro.** 549 valores de `SpO2` superiores a 100 % vienen marcados
  `OK`, mientras que las filas marcadas `CHECK` contienen mayormente valores normales. El sistema
  aplica su propio control de plausibilidad contra `variable_catalog` y trata `quality_flag` como
  una señal más.
- **166 filas de `TEMP` vienen en degF.** Sin convertir, el máximo de temperatura es 99,45.
- **540 filas son retransmisiones idénticas** de observaciones ya presentes, en 45 pacientes, y las
  540 caen dentro de una ventana de conectividad del mismo paciente. Se marcan como duplicadas y
  se les corrige la disponibilidad; no se descartan, porque son evidencia de calidad citable.
- **`connectivity_events.device_id` es 100 % wearables.** Unirlo con signos vitales por `device_id`
  devuelve cero filas: la unión válida es por paciente más contención temporal.
- **El muestreo es una grilla regular** —20 min para HR/RR/SpO2, 60 para TEMP, 120 para SBP/DBP— y
  no hay celdas vacías en ningún archivo. La cobertura de una ventana es un conteo exacto.

## Declaración de tecnologías

Desarrollo asistido con Claude Code (Anthropic). Ningún modelo generativo participa en el cálculo
de scores, la asignación de prioridad ni la selección de evidencia (principio P-09).
