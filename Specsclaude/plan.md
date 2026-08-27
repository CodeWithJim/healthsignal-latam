# Plan de implementación — HealthSignal LATAM

| | |
|---|---|
| **Versión** | 0.2 |
| **Fecha** | 2026-08-26 |
| **Estado** | Borrador para revisión |
| **Gobernado por** | [`constitution.md`](constitution.md) v1.0 |
| **Implementa** | [`spec.md`](spec.md) v0.2 |

Este documento describe **cómo** se construye lo que `spec.md` exige. Toda decisión de aquí es
revisable sin tocar el spec; ninguna puede violar la constitución.

---

## 1. Decisión central de arquitectura

**Pipeline vectorizado para el movimiento de datos, núcleo de dominio aislado para la decisión.**
La frontera se cruza una sola vez, por paciente y por instante de decisión.

```
data/raw/          los 17 CSV originales · sólo lectura · nunca se escriben
       ↓  ingest        adaptadores declarativos (RF-01)
capa CLEAN         unidades canónicas · dedupe · cuarentena · available_time (RD-02..RD-07)
       ↓  features
capa FEATURES      baseline por paciente/canal/bucket · cobertura (RF-04)
       ↓
╔══════════════════════════════════════════════════════════════════════╗
║  AsOfStore.snapshot(patient_id, T)                                   ║
║  única vía de lectura del núcleo · jamás devuelve available_time > T ║
╚══════════════════════════════════════════════════════════════════════╝
       ↓
núcleo de dominio  funciones puras · sin IO · Assessment, Signal, Evidence (RF-03..RF-13)
       ↓
capa RESULTS       tablas signals/evidence con constraints → CSV (RS-01..RS-06)
       ↓
api + ui           exploración y decisión en vivo (RF-14, RF-15)
```

**Por qué este corte y no Clean Architecture ortodoxa en todas las capas:** modelar cada una de las
1.622.969 observaciones como objeto de dominio para cruzar fronteras destruiría el rendimiento sin
aportar nada. Y **por qué no un pipeline puro:** en un pipeline la regla `available_time ≤ T` se
cumple por disciplina, y un `join` mal escrito la viola en silencio. Detrás del puerto, el leakage
deja de ser un bug que hay que buscar y pasa a ser inexpresable.

La línea cae donde cambia la naturaleza del trabajo: **cómputo sobre lotes** → infraestructura;
**decisión sobre un paciente en un instante** → dominio.

---

## 2. Stack

| Componente | Elección | Justificación |
|---|---|---|
| Lenguaje | **Python 3.11** | Mismo lenguaje que `validate_submission.py`: sin fricción de tipos, fechas ni serialización al exportar |
| Almacén analítico | **DuckDB** | Archivo único sin servidor; columnar; `ASOF JOIN` nativo —la primitiva exacta para cruzar fuentes con latencias distintas—; lee y escribe Parquet; el contrato de salida se expresa como constraints |
| Cálculo numérico | **NumPy + SciPy** | Suficientes para MAD y Theil–Sen sobre ventanas de 18 muestras |
| API | **FastAPI** | Tipado, OpenAPI automática, latencia adecuada para RNF-02 |
| Interfaz | Ver §7 | Decisión abierta NC-06 |
| Pruebas | **pytest** | — |

**Polars quedó fuera.** El plan v0.1 lo incluía por tipado estricto y `join_asof`, pero la ingesta
completa se resolvió en SQL sobre DuckDB, que ya aporta ambas cosas. Se incorporará sólo si alguna
etapa posterior lo necesita de verdad; agregar una dependencia que no se usa contradice el criterio
de calidad de ingeniería que la rúbrica evalúa.

**Entorno.** Virtualenv del proyecto en `.venv` (Python 3.11.3). Dependencias fijadas en
`requirements.txt`. El Python global de la máquina traía sólo `numpy` y `scipy`, y no admitía
instalación por un archivo bloqueado; el entorno aislado resuelve eso y además hace reproducible la
instalación (RNF-01).

---

## 3. Modelo de datos

Tres formas, todas con doble timeline. Las fuentes se normalizan hacia ellas.

### 3.1 Observaciones — punto en el tiempo con valor
Unifica `vital_signs` + `wearable_observations` + `device_observations` + `laboratory_results`.

```sql
CREATE TABLE observations (
  source_file      TEXT NOT NULL,        -- '03_monitoring/vital_signs.csv'
  record_id        TEXT NOT NULL,        -- 'OBS-0000000001'      → esto ES evidence.csv
  patient_id       TEXT NOT NULL,
  encounter_id     TEXT,
  device_id        TEXT,
  variable_code    TEXT NOT NULL,
  domain           TEXT NOT NULL,        -- VITAL | WEARABLE | LAB | DEVICE

  event_time       TIMESTAMP NOT NULL,
  available_time   TIMESTAMP NOT NULL,
  CHECK (available_time >= event_time),  -- P-02 como constraint de base

  value_num        DOUBLE,               -- unidad canónica
  value_text       TEXT,                 -- ACTIVITY_LEVEL, SLEEP_STATE
  value_raw        DOUBLE,               -- lo que decía el CSV
  unit_raw         TEXT,
  unit_canonical   TEXT,                 -- auditoría de la conversión (RD-04)

  source_system    TEXT,
  quality_flag     TEXT,                 -- el de origen, como señal, no como filtro
  is_plausible     BOOLEAN,              -- gate propio (RD-06)
  is_duplicate     BOOLEAN,              -- retransmisión (RD-05)
  ref_low          DOUBLE,
  ref_high         DOUBLE,
  PRIMARY KEY (source_file, record_id)
);
CREATE INDEX idx_asof ON observations(patient_id, variable_code, available_time);
```

### 3.2 Intervalos — rango con estado
Unifica `patient_context` + `connectivity_events` + `medication_administrations` + `encounters`.

```sql
CREATE TABLE intervals (
  source_file TEXT, record_id TEXT,
  patient_id  TEXT, device_id TEXT,
  kind        TEXT,   -- CONTEXT | CONNECTIVITY | MEDICATION | ENCOUNTER
  subtype     TEXT,   -- SLEEP_STATE | PHYSICAL_ACTIVITY | RECOVERY_PHASE | DISCONNECTED | ...
  value_text  TEXT,   -- SLEEP | LIGHT | MODERATE | HIGH | POST_ACTIVITY_RECOVERY
  start_time  TIMESTAMP, end_time TIMESTAMP, available_time TIMESTAMP,
  confidence  DOUBLE,
  extra_json  TEXT,   -- delayed_records, packet_loss_estimate, dose_value...
  PRIMARY KEY (source_file, record_id)
);
```

### 3.3 Hechos clínicos — antecedentes
```sql
CREATE TABLE clinical_facts (
  record_id TEXT PRIMARY KEY, source_file TEXT, patient_id TEXT,
  condition_category TEXT, onset_date DATE,
  recorded_datetime TIMESTAMP, available_time TIMESTAMP,
  status TEXT, severity_context TEXT, source_system TEXT
);
```
*Todos los `recorded_datetime` son anteriores al 2026-07-01: los antecedentes están disponibles
desde el primer instante de la ventana de estudio.*

### 3.4 Cuarentena y manifiesto
```sql
CREATE TABLE quarantine (
  source_file TEXT, record_id TEXT, patient_id TEXT, variable_code TEXT,
  reason TEXT,          -- IMPLAUSIBLE | DUPLICATE | UNPARSEABLE | UNIT_UNKNOWN
  detail TEXT, raw_row TEXT,
  PRIMARY KEY (source_file, record_id, reason)
);

CREATE TABLE ingest_manifest (
  run_id TEXT, source_file TEXT, sha256 TEXT, bytes BIGINT,
  rows_read BIGINT, rows_accepted BIGINT, rows_quarantined BIGINT,
  ingested_at TIMESTAMP, git_sha TEXT
);
```
`rows_read = rows_accepted + rows_quarantined` es la invariante de RF-02, verificable por consulta.

**La capa RAW no se copia.** Los 17 CSV originales son la capa RAW; el manifiesto registra su hash
y DuckDB los consulta en su lugar cuando hace falta mostrar la fila original (P-08). Los adaptadores
declaran `replayable`; una fuente no releíble sí aterrizaría en crudo, pero ninguna de las 17 lo es.

### 3.5 Salidas — el contrato como constraints
```sql
CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL,
  decision_datetime TIMESTAMP NOT NULL,
  risk_score DOUBLE NOT NULL, confidence_score DOUBLE,
  priority_level TEXT NOT NULL,
  evidence_start TIMESTAMP NOT NULL, evidence_end TIMESTAMP NOT NULL,
  explanation TEXT NOT NULL, model_version TEXT NOT NULL, run_id TEXT NOT NULL,
  CHECK (risk_score BETWEEN 0 AND 1),
  CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1),
  CHECK (priority_level IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  CHECK (evidence_start <= evidence_end AND evidence_end <= decision_datetime)
);

CREATE TABLE evidence (
  signal_id TEXT NOT NULL REFERENCES signals(signal_id),   -- evidencia huérfana imposible
  source_file TEXT NOT NULL, record_id TEXT NOT NULL, variable_code TEXT,
  event_datetime TIMESTAMP NOT NULL, available_datetime TIMESTAMP NOT NULL,
  evidence_role TEXT NOT NULL, contribution DOUBLE,
  CHECK (evidence_role IN ('PRIMARY','SUPPORTING','CONTEXT','QUALITY'))
);
```
Exportar es `COPY ... TO 'results/signals.csv' (HEADER, DELIMITER ',')`. **Pasar el validador deja
de ser una tarea y pasa a ser una consecuencia del esquema.**

---

## 4. Pipeline

| # | Etapa | Entrada → salida | Requisitos |
|---|---|---|---|
| 0 | **RAW** | Los 17 CSV + `ingest_manifest` con SHA-256 | P-08, RNF-04 |
| 1 | **INGEST** | CSV → filas normalizadas vía adaptador declarativo | RF-01, RD-02, RD-03 |
| 2 | **CLEAN** | Clasificación `accepted` \| `quarantine` | RF-02, RD-04..RD-07 |
| 3 | **FEATURES** | Baseline y cobertura por paciente/canal/bucket | RF-04, RD-08 |
| 4 | **DETECT** | Dictamen en cada instante evaluado | RF-06..RF-10 |
| 5 | **EVENTIZE** | Dictámenes → señales por cambio material de estado | RF-11 |
| 6 | **EXPORT** | Tablas → `signals.csv` + `evidence.csv` | RS-01..RS-06 |
| 7 | **AUDIT** | Validador oficial + leakage + métricas | CE-01..CE-08 |

### Forma del adaptador (etapa 1)
```yaml
- source_file: 03_monitoring/wearable_observations.csv
  record_id:   wearable_observation_id
  patient:     patient_id
  device:      device_id
  event_time:  timestamp
  available_time: sync_datetime          # regla oficial
  variable:    variable_code
  value:       value                     # mixto: numérico y categórico
  unit:        unit
  quality:     measurement_quality
  domain:      WEARABLE
  replayable:  true
```
Una fuente nueva es una declaración más, no código nuevo (RF-01).

---

## 5. El analizador

Función pura: recibe un snapshot en `(paciente, T)` y devuelve un dictamen. No lee archivos, no
consulta la base, no conoce el reloj.

### 5.1 Dos ventanas que no se tocan
```
baseline    [T − 54h , T − 6h]     144 muestras esperadas de HR
evidencia   [T −  6h , T     ]      18 muestras esperadas de HR
```
Que no se solapen es lo que impide que el evento contamine su propia referencia (RF-04). Como la
grilla es de 20 min exactos, la cobertura es un conteo exacto (RD-08).

### 5.2 Normalización contra el propio paciente
```
m = mediana(baseline)
s = max( MAD(baseline) × 1.4826 , piso_canal )
```
El piso evita que un paciente muy estable (MAD ≈ 0) produzca desviaciones infinitas. Se deriva del
percentil bajo de la distribución poblacional de MAD, medido, no supuesto (NC-05).

### 5.3 Tres medidas por canal
```
nivel        = (mediana(último tercio de la evidencia) − m) / s
pendiente    = TheilSen(t, v) sobre la ventana, en unidades de s por hora
persistencia = fracción de muestras del lado del deterioro y por encima de 1

s_c = dir_c × (0.5·nivel + 0.5·pendiente×6h) × persistencia      → recortado a [0, techo]
```

| Solo esto | Qué falla |
|---|---|
| Nivel | Detecta el escalón, no la trayectoria. Llega a las 11:00, no a las 09:00 |
| Pendiente | Dispara con ruido en ventanas cortas |
| Sin persistencia | Un punto extremo arrastra nivel y pendiente a la vez |

**Theil–Sen y no mínimos cuadrados:** es la mediana de las pendientes entre pares de puntos; un
outlier no la mueve. Con 18 muestras son 153 pares. Es lo que impide que un artefacto de `SpO2`
fabrique una tendencia inexistente.

### 5.4 Concordancia
```
k = #{ c : s_c ≥ 1.0 }
S = ( Σ w_c · s_c ) · C(k) · cobertura  +  λ · soporte_multifuente
```
El **recorte por canal antes de agregar** es el mecanismo entero: con techo 6 y multiplicadores
`C(1)=0.40 · C(2)=0.80 · C(3)=1.15 · C(4)=1.40`, cuatro canales a 3σ dan 16,8 y un canal a 10σ da
2,4. Sin el recorte la fórmula contradiría la tesis del reto (RF-06, P-07).

### 5.5 Supresión
Cada regla es `(snapshot, dictamen) → Supresión | None`, y cada supresión emite su fila de evidencia
con el `record_id` que la justifica (RF-08, P-04).

| Regla | Disparo | Efecto |
|---|---|---|
| `actividad` | Ventana solapa PHYSICAL_ACTIVITY HIGH/MODERATE o RECOVERY_PHASE **y** la desviación está dominada por HR | demote fuerte · cita `context_id` |
| `calidad` | ≥ 30 % de las muestras que empujan el score son LOW_SIGNAL o implausibles | demote muy fuerte · cita `observation_id` |
| `transitorio` | Persistencia menor a 2 muestras consecutivas | demote fuerte |
| `cobertura` | Cobertura de la ventana bajo el mínimo | tope de prioridad + confianza baja · cita `event_id` |

### 5.6 Score, confianza y prioridad
```
S'         = S · Π (1 − d_i)
risk_score = 1 − exp( −S' / k₀ )
confidence = cobertura × calidad × frescura
```
Ejes independientes (RF-09). Las bandas de prioridad son compuertas con condiciones duras, no
cortes del score (RF-10):

| Prioridad | Condición |
|---|---|
| `CRITICAL` | `risk ≥ 0.85` ∧ `k ≥ 3` ∧ persistencia alta ∧ cobertura suficiente ∧ sin supresión activa |
| `HIGH` | `risk ≥ 0.65` ∧ `k ≥ 2` |
| `MEDIUM` | `risk ≥ 0.40` |
| `LOW` | resto, o demotado por supresión |

Las señales `LOW` que se emiten son las que una supresión bajó desde una banda superior: son la
demostración del control de falsas alertas, no ruido.

Todas las constantes viven en `config/scoring.yaml` (RNF-05) y se calibran tras la primera corrida
(NC-02). *Referencia de la sonda del 2026-08-26: mediana 4,28 · p95 16,80 · máximo ≈ 27,3, con 118
pacientes alcanzando concordancia de cuatro canales.*

### 5.7 Eventización
Evaluación en grilla horaria dentro del encuentro del paciente (NC-01). Emisión sólo ante primer
ingreso a una banda o escalamiento, con período refractario por paciente (RF-11).

### 5.8 Orden de construcción
Cada escalón se verifica antes del siguiente: un canal y un paciente → seis canales → concordancia
(CA-01 debe pasar) → supresión (CA-02..CA-04 deben pasar) → grilla completa.

---

## 6. Estructura del repositorio

```
healthsignal/
├─ constitution.md · spec.md · plan.md · README.md · ARCHITECTURE.md
├─ config/
│  ├─ sources/*.yaml           adaptadores declarativos
│  └─ scoring.yaml             constantes de calibración
├─ data/
│  ├─ raw/                     los 17 CSV · sólo lectura
│  └─ warehouse.duckdb         CLEAN + FEATURES + RESULTS
├─ src/hs/
│  ├─ ingest/                  adaptadores, unidades, dedupe, plausibilidad
│  ├─ timeline/                AsOfStore  ← el puerto
│  ├─ domain/                  Assessment, Signal, Evidence, scoring · SIN IO
│  ├─ detect/                  ejecución sobre la grilla
│  ├─ export/                  COPY a CSV
│  └─ api/                     FastAPI
├─ tests/
│  ├─ test_trajectories.py     CA-01..CA-06 · trayectorias sintéticas
│  ├─ test_no_leakage.py       CA-07 · store espía
│  ├─ test_contract.py         ejecuta validate_submission.py
│  └─ test_ingest.py           invariante rows_read = accepted + quarantined
├─ results/                    signals.csv · evidence.csv
└─ ui/
```

**Git:** rama `dev` desde el primer commit; `master` sólo recibe merges. Ninguna sesión trabaja
directamente sobre `master`.

---

## 7. Mecanismo de exploración — decisión abierta (NC-06)

| Opción | A favor | En contra |
|---|---|---|
| **FastAPI + SPA mínima** *(recomendada)* | Menor riesgo; el diferenciador es `/decide`, no el framework; la guía confirma que el frontend no es obligatorio | Menos vistoso |
| FastAPI + Next.js | Lo que proponía `spec.txt` v0.1 | Horas que se le sacan al motor salvo que alguien lo escriba sin pensarlo |
| Sólo API + CLI | Válido según la guía | Pierde el timeline con la ventana de evidencia sombreada |

**El endpoint que decide los puntos difíciles (RF-14):**
```
GET /decide?patient=PAT-0869&at=2026-07-20T12:00:00
```
El evaluador elige cualquier paciente y cualquier instante. El motor corre en vivo con la garantía
as-of y devuelve score, prioridad, explicación, supresiones activas y las filas fuente exactas.
Nada precargado. La rúbrica reserva puntos para «validación técnica con caso oficial no preparado»
y la guía avisa que los evaluadores pueden pedir una situación nueva.

---

## 8. Validación y métricas

Sin Gold Standard público, el conjunto de negativos se construye desde los marcadores públicos de
RF-08. Eso permite reportar tasa de falsas alertas defendible.

**Tabla de ablación** — el artefacto central del pitch:

| Configuración | Señales HIGH+ | Impacto en distractores | Anticipación |
|---|---|---|---|
| Sólo umbrales fijos | — | — | — |
| + baseline personal | — | — | — |
| + concordancia multicanal | — | — | — |
| + ledger de supresión | — | — | — |

Cada fila que se agrega debe reducir falsas alertas **sin perder anticipación**. Los valores se
completan al correr; la estructura es el argumento (CE-06).

Se reportan además CE-01 a CE-08, y se auditan manualmente varias señales altas antes de cerrar.

---

## 9. Fases

| Fase | Entrega verificable |
|---|---|
| **0 · Setup** | Repo en `dev`, entorno instalado, los 17 CSV cargados a CLEAN con el `CHECK` puesto y el manifiesto de hashes cuadrando |
| **1 · Revisión inicial (Día 1)** | `AsOfStore` funcionando y un paciente recorrido de punta a punta. `test_no_leakage` en verde |
| **2 · Avance (Día 1)** | Motor de concordancia, primeras señales con evidencia, `validate_submission.py` sin errores |
| **3 · Consolidación (Día 2)** | Ledger de supresión, conjunto de negativos, tabla de ablación, interfaz de exploración |
| **4 · Final (Día 2)** | `/decide` en vivo, README con las ocho secciones exigidas, diagrama que corresponde a lo implementado, declaración de tecnologías |

**Regla de cierre de fase:** una fase no termina con código escrito, sino con su criterio de
aceptación pasando.

---

## 10. Mapa a la rúbrica

| Criterio | Pts | Qué lo cubre |
|---|---|---|
| Arquitectura y calidad técnica | 20 | Corte pipeline/dominio · puerto as-of · contrato como constraints · pruebas · RNF-06 |
| Funcionamiento del prototipo | 20 | End-to-end sobre los 17 CSV + `/decide` en vivo |
| Innovación | 15 | Concordancia sobre magnitud · leakage inexpresable · negativos desde marcadores públicos |
| Impacto y escalabilidad | 10 | Adaptadores declarativos (RF-01) · opera con conectividad intermitente por diseño |
| Integración y análisis temporal | 6 | Cinco fuentes con latencias distintas alineadas as-of · RF-07 |
| Identificación de señales | 8 | RF-06 · baseline individual |
| Priorización | 6 | RF-10, RF-13 |
| Falsas alertas y robustez | 5 | RF-08 · tabla de ablación · CE-04 |
| Explicabilidad y trazabilidad | 5 | RF-12, RF-15 · evidencia generada por el cómputo |
| Caso no preparado / pitch | 5 | RF-14. *Los dos documentos oficiales difieren en estos 5 puntos: la guía los asigna a validación con caso oficial no preparado y el documento del desafío a pitch y demostración. Se cubren ambos* |

**Pasar `validate_submission.py` vale cero puntos:** es una compuerta, no un criterio. Conviene
tenerlo presente al repartir horas — arquitectura y funcionamiento suman 40 de 100.

---

## 11. Decisiones descartadas

| Descartado | Por qué |
|---|---|
| Modelo entrenado en la ruta de decisión | No hay etiquetas públicas; un detector no supervisado es una fábrica de falsas alertas; la guía penaliza «alertas con IA sin evidencia rastreable»; un score no es evidencia |
| Modelo generativo procesando toda la data | Para llamarlo hay que decidir primero qué enviarle, y esa decisión ya es el detector. Además: ~144.000 evaluaciones × ~4k tokens ≈ 576 M tokens por corrida, no determinista, e incompatible con `record_id` exactos |
| Streaming, colas, microservicios | El dataset es estático y congelado. Sobreingeniería que cuesta horas del motor |
| Copiar los CSV a tablas `DataRaw` | Duplica 240 MB y crea un segundo «original» que puede divergir. La trazabilidad se cumple con `(source_file, record_id)` + manifiesto de hashes |
| Polars justificado por escala | El barrido completo tomó 30,6 s en Python puro. Se usa por tipado y `join_asof`, no por volumen |
| Filtrar por `quality_flag != OK` | Descarta 4.164 filas buenas y conserva 549 valores imposibles marcados `OK` (RD-06) |

**Donde sí entra un modelo generativo** —fuera de la ruta de evidencia (P-09)— como capa narrativa
sobre la explicación determinista, como traductor de consultas en lenguaje natural a SQL contra
nuestras tablas (mostrando el SQL), y como segunda opinión sobre el top-N ya rankeado. Costo
acotado, trazable, y declarado.

---

## 12. Riesgos

| Riesgo | Mitigación |
|---|---|
| El dataset se declara **`Candidate 1 — not yet frozen`** pese a coincidir con el manifiesto | Toda constante en configuración (RNF-05). Recalibrar debe ser correr un script, no reescribir el motor |
| Calibración sobre una sola distribución observada | Bandas expresadas en desviaciones del propio paciente, no en valores absolutos |
| El volumen de señales se dispara | RF-11 con período refractario; CE-08 como control |
| La interfaz consume horas del motor | NC-06 se decide antes de la Fase 3; la rúbrica dice que una interfaz avanzada no compensa la ausencia de análisis o trazabilidad |
