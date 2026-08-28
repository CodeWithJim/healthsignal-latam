# HealthSignal LATAM — Motor de Concordancia

Sistema de apoyo a la decisión que convierte los 2.552.104 registros heterogéneos de la red
sintética RISA en señales de riesgo priorizadas, explicables y trazables hasta su fila de origen.

**No emite diagnósticos ni prescripciones.** Detecta y prioriza situaciones que ameritan revisión
profesional.

> **La tesis.** No detectamos valores altos. Detectamos **varios canales fisiológicos moviéndose
> juntos, en la dirección clínicamente coherente, de forma sostenida** — medidos contra el propio
> historial del paciente y usando exclusivamente información disponible en el instante de la
> decisión.

| | |
|---|---|
| **Resultado** | 210 señales sobre 172 pacientes · 3.405 filas de evidencia |
| **Validador oficial** | `VALID SUBMISSION FORMAT` · 0 errores · 0 warnings |
| **Causalidad temporal** | 0 violaciones en 3.405 filas de evidencia |
| **Anticipación** | mediana 4,0 h antes del máximo posterior |
| **Código** | Python + HTML/JS · 84 pruebas automatizadas |

---

## 1 · Descripción de la solución

RISA entrega información de un mismo paciente desde cinco familias de fuentes que no comparten
frecuencia, unidad, estructura ni instante de captura: signos vitales cada 20 minutos, wearables
cada 30, laboratorios episódicos que se informan hasta 6 horas después de la toma, antecedentes
clínicos, y contexto de actividad y conectividad.

El sistema recorre siete etapas:

```
RAW → CLEAN → FEATURES → DETECT → EVENTIZE → EXPORT → AUDIT
```

Homogeneiza las 17 fuentes preservando la procedencia registro a registro, reconstruye para cada
paciente **qué se sabía en cada instante**, mide la desviación de cada canal contra el historial
de ese mismo paciente, y emite una señal cuando varios canales se mueven de forma concordante y
sostenida. Cada señal viaja con la lista exacta de registros que la sustentan y con las hipótesis
alternativas que se evaluaron y descartaron.

**Los documentos que gobiernan el diseño** están en [`docs/specs/`](docs/specs/):
[`constitution.md`](docs/specs/constitution.md) (nueve principios innegociables),
[`spec.md`](docs/specs/spec.md) (requisitos verificables) y
[`plan.md`](docs/specs/plan.md) (implementación).

## 2 · Arquitectura general

El diagrama completo, con la correspondencia entre cada componente y su archivo, está en
[**`ARCHITECTURE.md`**](ARCHITECTURE.md).

```
data/raw/          los 17 CSV originales · sólo lectura · hash verificado en cada corrida
       ↓  ingest        adaptadores declarativos en config/sources.yaml
capa CLEAN         unidades canónicas · deduplicación · plausibilidad · available_time
       ↓
   AsOfStore.snapshot(patient_id, T)   ← única vía de lectura del núcleo
       ↓
núcleo de dominio  funciones puras · sin IO · concordancia, supresión y prioridad
       ↓
capa RESULTS       signals ← FK ─ evidence → signals.csv · evidence.csv
       ↓
API + interfaz     /decide en vivo · /source-row hasta el CSV original
```

**La decisión de arquitectura central:** pipeline vectorizado para el movimiento de datos, núcleo
de dominio aislado para la decisión. La frontera se cruza una sola vez, por paciente y por
instante.

### Estructura del repositorio

Cada carpeta corresponde a una posición en el diagrama de arriba:

| Carpeta | Papel en la arquitectura |
|---|---|
| `src/hs/domain/` | **Núcleo.** `models.py` y `scoring.py`: funciones puras, sin IO ni SQL |
| `src/hs/timeline/` | **El puerto as-of.** `AsOfStore` — única vía de lectura del núcleo |
| `src/hs/narrative/` | Narrativa opcional con OpenAI, posterior al dictamen y con fallback local |
| `src/hs/ingest.py` · `manifest.py` · `schema.sql` | Adaptador de entrada: RAW → CLEAN, hashes y restricciones |
| `src/hs/detect/` | Caso de uso: recorre la grilla, cruza el puerto, eventiza |
| `src/hs/export/` · `src/hs/api/` | Adaptadores de salida: CSV de entrega y HTTP |
| `src/hs/paths.py` | Único lugar donde se resuelven ubicaciones en disco |
| `config/` | Calibración y adaptadores declarativos. Una fuente nueva es una entrada en YAML |
| `scripts/` | Entrypoints, uno por etapa del pipeline (`00_ingest` … `06_caso`) |
| `tests/` · `ui/` | Criterios de aceptación · interfaz sin dependencias |
| `data/` · `results/` | Entradas y salidas. No se versionan |
| `reto/` | Material recibido de la organización, intacto. Ver [`reto/README.md`](reto/README.md) |
| `docs/specs/` | Los documentos que gobiernan el diseño |

La separación que importa es la de las dos últimas filas contra el resto: **lo que construimos
nosotros y lo que nos dieron no se mezclan en la raíz.**

**La regla temporal es una restricción de base de datos, no una convención.** `observations`
declara `CHECK (available_time >= event_time)` y `evidence` declara clave foránea contra
`signals`: la evidencia huérfana que el validador oficial rechaza es imposible de insertar. Hay
pruebas que intentan violar ambas y esperan la excepción.

## 3 · Tecnologías utilizadas

| Componente | Elección | Por qué |
|---|---|---|
| Lenguaje | **Python 3.11** | Mismo lenguaje que `validate_submission.py`: sin fricción de tipos ni fechas al exportar |
| Almacén analítico | **DuckDB 1.5** | Archivo único sin servidor; columnar; lee y escribe Parquet; el contrato de salida se expresa como constraints |
| Cálculo | **NumPy 2.4** | Series vectorizadas con procedencia por muestra en el mismo índice |
| API | **FastAPI 0.141** | Tipado, OpenAPI automática, latencia adecuada para decisión en vivo |
| Narrativa | **OpenAI GPT-5.6 Terra** | Traduce el dictamen estructurado a lenguaje humano sin decidir riesgo ni prioridad |
| Interfaz | HTML + JS sin dependencias | Lo que se evalúa es la utilidad, no el framework |
| Pruebas | **pytest 9.1** | 84 pruebas automatizadas, incluida la ejecución del validador oficial |

**Sin Polars, sin pandas.** El plan inicial incluía Polars por `join_asof` y tipado estricto, pero
la ingesta se resolvió en SQL sobre DuckDB, que ya aporta ambas cosas. Agregar una dependencia que
no se usa contradice el criterio de calidad de ingeniería.

## 4 · Instalación y ejecución

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Los 17 CSV de RISA Data V1.0 van en `data/raw/`, conservando la estructura del paquete original
(`01_master/`, `02_clinical/`, `03_monitoring/`, `04_context/`, `05_metadata/`). No se versionan:
pesan 244 MB y su integridad se comprueba contra `MANIFEST_SHA256.txt` en cada corrida.

```bash
# Etapas 0-2 · RAW → CLEAN                          (~40 s)
.venv\Scripts\python.exe scripts\00_ingest.py

# Etapas 4-7 · barrido, eventización, export, auditoría   (~6 min)
.venv\Scripts\python.exe scripts\02_detect.py

# Criterios de aceptación
.venv\Scripts\python.exe -m pytest tests -q
```

La ingesta **descarta y reconstruye** la capa limpia en cada corrida, y con ella las señales
(`signals`, `evidence` y los CSV de `results/`): son derivadas de esa capa, y sobrevivir a una
reingesta las convertiría en señales de aspecto normal que ya no corresponden a los datos
cargados. Hay que volver a correr `02_detect.py` después de cada ingesta. Lo único acumulativo
es `ingest_manifest`, que es el registro de qué se cargó y cuándo.

## 5 · Dependencias principales

`duckdb 1.5.5` · `numpy 2.4.6` · `PyYAML 6.0.3` · `fastapi 0.141.1` · `uvicorn 0.52.4` ·
`pytest 9.1.1` · `httpx 0.28.1` · `openai 3.5.0` · `python-dotenv 1.2.3`. Versiones fijadas en
[`requirements.txt`](requirements.txt).

## 6 · Reproducir la demostración

```bash
# Un paciente recorrido a través del puerto as-of
.venv\Scripts\python.exe scripts\01_snapshot.py PAT-0869 2026-07-20T18:00:00

# Métricas sin Gold Standard
.venv\Scripts\python.exe scripts\03_metrics.py

# Tabla de ablación
.venv\Scripts\python.exe scripts\04_ablation.py --pacientes 250

# Interfaz y API de decisión en vivo   →   http://127.0.0.1:8000/
.venv\Scripts\python.exe scripts\05_serve.py
```

La interfaz siempre muestra una lectura humana fundamentada. La ubicación exacta para la clave es
`D:\Dev\HackatonIA2026Claude\.env`. Crear el archivo a partir de la plantilla y editar solamente
`OPENAI_API_KEY`:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Abrir .env y reemplazar: OPENAI_API_KEY=pega_aqui_tu_clave_de_openai
.venv\Scripts\python.exe scripts\05_serve.py
```

`OPENAI_MODEL=gpt-5.6-terra` ya está configurado como valor predeterminado. La variable sólo es
necesaria en `.env` si se quiere probar otro modelo. El prompt predeterminado está en
`src/hs/narrative/service.py`, constante `DEFAULT_PROMPT`; exige una explicación breve de qué ocurre,
qué revisar y qué registros sustentan el texto, sin diagnóstico ni recomendaciones terapéuticas.

Sin clave —o si el servicio externo falla— se utiliza automáticamente una narrativa local basada en
los mismos datos reales. El cálculo de riesgo y prioridad no depende de OpenAI.

Con la aplicación en marcha, el interruptor **IA** en el encabezado permite activar o desactivar
OpenAI sin reiniciar el servidor. Al desactivarlo, las consultas nuevas usan el resumen local y no
consumen la API; la clave nunca se envía al navegador. El ajuste dura mientras el servidor está
encendido y vuelve al valor inicial configurado al reiniciarlo.

### Decisión en un instante arbitrario

```
GET /decide?patient=PAT-0869&at=2026-07-20T18:00:00&narrative=true
```

El evaluador elige **cualquier paciente y cualquier instante** y el motor computa en el momento,
usando exclusivamente evidencia con `available_time <= at`. Devuelve puntaje, prioridad,
confianza, la contribución de cada canal con su baseline, las reglas evaluadas con su cita, y las
filas fuente exactas. Con `narrative=true`, añade una síntesis generada que indica qué muestran los
datos y qué aspectos ameritan revisión. Los valores, la prioridad y las referencias permanecen
deterministas y separados del texto generado. **Nada precargado.**

### Historia hasta un corte o encuentro completo

```text
GET /patients/PAT-0002/history-analysis?hasta=2026-07-13T12:20:00&narrative=true
GET /patients/PAT-0869/history-analysis?narrative=true
```

Con `hasta`, el motor recorre el encuentro desde su inicio hasta ese corte. Sin `hasta`, analiza
el encuentro completo. Cada punto se vuelve a evaluar con la misma garantía causal de `/decide`:
una evaluación interna en `T` nunca observa registros disponibles después de `T`.

La respuesta mantiene separados dos resultados que no son intercambiables:

- **Estado al cierre:** decisión puntual exactamente en el final solicitado.
- **Máxima prioridad histórica:** peor episodio encontrado al recorrer el período cada 20 minutos.

Por eso una historia puede contener un episodio `CRITICAL` y cerrar en `LOW`; la aplicación no
presenta un episodio pasado como si fuera el estado final. La interfaz grafica todo el historial
disponible hasta el corte, muestra los seis canales aunque su contribución sea cero, incorpora los
laboratorios disponibles y sombrea las seis horas que sustentan el episodio máximo.

Desde `/source-row?source_file=…&record_id=…` se llega a la fila tal cual está en el CSV original.
`source_file` se valida contra la lista de fuentes cargadas: la ruta nunca se construye con texto
libre del pedido.

### Auditar una señal

La interfaz muestra el ranking, el historial completo de los canales, la evolución del riesgo, la
ventana del episodio máximo sombreada, la tabla de contribuciones de ese episodio y la evidencia
agrupada por rol con cada `record_id` enlazado a su fila original en RISA.

## 7 · Mecanismo de análisis y priorización

### Ventanas

Dos ventanas que **nunca se solapan**: baseline `[T−54h, T−6h]` y evidencia `[T−6h, T]`. Que no se
toquen impide que el evento contamine su propia referencia — la trampa que el Documento Técnico
Maestro nombra como *"baselines construidos con el futuro"*.

### Tres medidas por canal

Contra la mediana y la MAD del propio paciente, con un piso de dispersión derivado del percentil 5
de la distribución poblacional:

| Medida | Qué captura | Por qué no alcanza sola |
|---|---|---|
| **Nivel** | Desplazamiento del último tercio contra el baseline | Detecta el escalón, no la trayectoria. Llega tarde |
| **Pendiente** (Theil–Sen) | Deriva sostenida en la ventana | Dispara con ruido en ventanas cortas |
| **Persistencia** (racha) | Que la desviación se sostenga | Sin ella, un punto extremo arrastra las otras dos |

Theil–Sen es la mediana de las pendientes entre todos los pares de puntos: un valor atípico no la
mueve, a diferencia de mínimos cuadrados. Es lo que impide que un artefacto de una muestra fabrique
una tendencia inexistente.

### Concordancia

```
s_c = clip( 0,5·nivel + 0,5·pendiente , 0 , 6 ) × persistencia
k   = canales con s_c ≥ 1
S   = ( Σ w_c · s_c ) × C(k)  +  λ · corroboración de laboratorio
```

**El recorte por canal antes de agregar es el mecanismo entero.** Con techo 6 y multiplicadores
`C(1)=0,40 · C(2)=0,80 · C(3)=1,15 · C(4)=1,40`, cuatro canales moderados superan a uno extremo
por construcción, no por ajuste de umbrales.

### Riesgo, confianza y prioridad

`risk = 1 − e^(−S′/k₀)` tras aplicar las supresiones. `confidence = cobertura × calidad × frescura`
es un **eje independiente**: la ausencia de datos reduce la confianza y nunca incrementa el riesgo.
Un paciente desconectado no está menos grave por estar desconectado — está peor observado, queda
con tope de prioridad y su `connectivity_events.event_id` citado como evidencia `QUALITY`.

Las bandas son **compuertas con condiciones duras**, no cortes del puntaje:

| Prioridad | Condición |
|---|---|
| `CRITICAL` | `risk ≥ 0,85` ∧ `k ≥ 3` ∧ persistencia ∧ cobertura ∧ **ninguna supresión activa** |
| `HIGH` | `risk ≥ 0,65` ∧ `k ≥ 2` |
| `MEDIUM` | `risk ≥ 0,40` |
| `LOW` | resto, o demotada por supresión |

Un solo canal jamás alcanza `CRITICAL`, por más extremo que sea.

### Control de alertas irrelevantes

Cada regla que se activa **emite su propia fila de evidencia** con el `record_id` que la justifica.
Y cuando el contexto está presente pero **no** explica el patrón, se registra igual con fuerza
cero: haber evaluado una hipótesis alternativa y haberla descartado es una decisión, y las
decisiones no se toman en silencio.

| Regla | Disparo | Efecto |
|---|---|---|
| `actividad` | Ventana solapa actividad HIGH/MODERATE o recuperación **y** la desviación está dominada por HR | −70 % · cita `context_id` |
| `actividad_evaluada` | Contexto presente pero el patrón es multicanal | registro · cita `context_id` |
| `calidad` | ≥30 % de las muestras que empujan el puntaje son implausibles | −90 % · cita `observation_id` |
| `transitorio` | La desviación no persiste más de una muestra | −80 % |
| `cobertura` | Cobertura de la ventana bajo el mínimo | tope de prioridad · cita `event_id` |

### Eventización

Evaluar no es emitir. El analizador dictamina cada hora dentro del encuentro del paciente; sólo se
emite señal ante el **primer ingreso a una banda o un escalamiento**, con período refractario de
12 horas. Sin esa distinción, `signals.csv` tendría 95.731 filas en vez de 264.

## 8 · Resultados y métricas

Corrida completa: **95.731 evaluaciones sobre 1.000 pacientes en 6,1 minutos.**

```
210 señales · 172 pacientes · 3.405 filas de evidencia (16,2 por señal)

CRITICAL   23        HIGH   33        MEDIUM  149        LOW    5
```

| Criterio | Objetivo | Resultado |
|---|---|---|
| Causalidad temporal | 100 % | **0 violaciones** en 3.405 filas |
| Cobertura de evidencia | 100 % | **0 señales sin evidencia**, 0 huérfanas |
| Validador oficial | 0 errores | **VALID SUBMISSION FORMAT**, 0 warnings |
| Impacto en distractores | ≈ 0 | **0,0 %** (0 de 56 señales HIGH+) |
| Anticipación | > 2 h | **mediana 4,0 h** (p25 1,0 · p75 10,8) |
| Integración multifuente | ≥ 1 | **35 señales** con 3 fuentes distintas |
| Volumen | 10²–10³ | **210 señales** |

**Corroboración de laboratorio: 18 señales.** Sólo 52 de los 4.593 resultados de laboratorio de
RISA están fuera de su rango de referencia (1,1 %), así que la corroboración multifuente es rara
por construcción. Los laboratorios normales se citan igual —haberlos mirado también es evidencia—
pero con rol `CONTEXT` y contribución cero: si contara la mera existencia de un laboratorio, el
bono sería una constante para todo paciente con resultados recientes y no corroboraría nada.

Las auditorías corren sobre los CSV exportados, no sobre el motor que los produjo. Ninguna señal
cita un `record_id` inexistente.

**Sin Gold Standard público**, el conjunto de negativos se construye desde los marcadores que el
propio escenario declara: contexto de actividad y recuperación, `quality_flag` `LOW_SIGNAL`,
valores fuera de plausibilidad, retransmisiones y eventos de conectividad.

### Ablación · qué aporta cada mecanismo

Sobre 250 pacientes y 24.605 evaluaciones, apagando un mecanismo por vez:

| Configuración | Señales | HIGH+ | de 1 canal | distractor | anticipación | pacientes |
|---|---|---|---|---|---|---|
| 0 · magnitud sin recorte | 117 | 13 | 0 | 0 | 4,3 h | 13 |
| 1 · + recorte por canal | 114 | 13 | 0 | 0 | 4,0 h | 13 |
| 2 · **+ persistencia** | **81** | 13 | 0 | 0 | 4,0 h | 13 |
| 3 · **+ concordancia** | 80 | **20** | 0 | 1 | 4,0 h | **16** |
| 4 · **+ supresión** (completo) | 80 | 20 | 0 | **0** | 4,0 h | 16 |

- **La persistencia elimina 33 señales (−29 %) sin costo de detección.** HIGH+ se mantiene en 13
  y la anticipación en 4,0 h: lo que se va son transitorios.
- **La concordancia sube las detecciones de 13 a 20 (+54 %) y los pacientes de 13 a 16**, sin
  perder anticipación. No filtra: encuentra deterioros multicanal que la magnitud sola subestima.
- **La supresión elimina el último distractor sin registrar**, sin tocar el volumen de HIGH+.

Lo que la tabla **no** demuestra, y conviene decirlo: la columna «de 1 canal» da cero en todas las
configuraciones, incluso sin recorte. En RISA una sola variable desviada no alcanza el umbral de
HIGH ni siquiera sin la protección. El recorte por canal es una garantía estructural que en estos
datos nunca tuvo que actuar; su efecto está medido sobre trayectorias sintéticas, no aquí.

### Auditoría manual de señales altas

El Documento Técnico Maestro recomienda auditar a mano algunas señales altas antes de cerrar. Se
hizo de forma sistemática, buscando los patrones de los que hay que sospechar. Tres hallazgos:

**Los pares de señales separadas por una hora son escalamientos, no repeticiones.** `PAT-0374` pasa
por MEDIUM 0,608 → HIGH 0,844 → CRITICAL 0,915 en tres horas. Un escalamiento de banda es un cambio
material de prioridad, que es exactamente lo que la política de eventización define como emisible.

**Un canal sin pendiente calculable llegaba al aporte máximo.** Una señal CRITICAL tenía a `TEMP`
como canal dominante con sólo 4 muestras en la ventana. Con menos de cinco puntos la pendiente no
se estima, así que el canal aporta sólo nivel — pero podía tocar el techo completo igual.
Corregido: **sin pendiente, medio techo.** Media evidencia, medio aporte.

**El arranque de la evaluación se midió, no se supuso.** 31 de 56 señales altas nacían en el primer
instante evaluable, lo que sugería que un warmup más corto ganaría anticipación. Se probó:

| warmup | baseline real | señales | distractores | anticipación |
|---|---|---|---|---|
| 18 h | ~12 h | 503 | 1,3 % | 1,8 h |
| 30 h | ~24 h | 392 | 0,8 % | 2,0 h |
| **54 h** | **48 h** | **210** | **0,0 %** | **4,0 h** |

Lo contrario de lo esperado, y monótono en las tres columnas. Un baseline de 12 horas no cubre un
ciclo diurno, así que la variación circadiana normal pasa a parecer desviación: más señales, peores,
y detectadas más tarde. El warmup se mantiene en 54 h.

### Criterios de aceptación del motor

Seis trayectorias sintéticas, sin leer un CSV. Si estas formas no se distinguen, lo que el motor
produzca sobre los datos reales no importa.

| Caso | k | Riesgo | Prioridad |
|---|---|---|---|
| Trayectoria oficial 08:00→11:00, evaluada a las **10:00** | 4 | **0,780** | HIGH |
| La misma, a las 11:00 | 4 | 0,922 | CRITICAL |
| **Un canal a +8,4 desviaciones sostenidas** | 1 | **0,183** | LOW |
| Pico de HR con contexto de actividad | 1 | 0,039 | LOW · supresión citada |
| Caída aislada de SpO2 a 71 % | 0 | 0,000 | LOW |
| Ruido plano | 0 | 0,000 | LOW |

La trayectoria del documento oficial dispara **a las 10:00**, una hora antes de que ninguna
lectura individual cruce un umbral convencional. Y cuatro canales moderados puntúan **4,3 veces
más** que uno solo desviado al extremo.

## 9 · Hallazgos sobre los datos que condicionaron el diseño

Verificados sobre RISA Data V1.0 Candidate 1, con los 17 archivos coincidiendo con el manifiesto.

- **`quality_flag` no sirve como filtro.** 549 valores de `SpO2` superiores a 100 % vienen
  marcados `OK`, mientras que las 4.164 filas marcadas `CHECK` contienen mayormente valores
  normales. Filtrar por `quality_flag` descarta datos buenos y conserva imposibles. El sistema
  aplica su propio control contra `variable_catalog` y trata `quality_flag` como una señal más.
- **166 filas de `TEMP` vienen en degF.** Sin convertir, el máximo de temperatura es 99,45.
- **540 filas son retransmisiones idénticas**, en 45 pacientes, y las 540 caen dentro de una
  ventana de conectividad del mismo paciente. Se les corrige la disponibilidad al cierre de esa
  ventana y se marcan duplicadas; no se descartan, porque son evidencia de calidad citable.
- **`connectivity_events.device_id` es 100 % wearables.** Unirlo con signos vitales por
  `device_id` devuelve cero filas: la unión válida es por paciente más contención temporal.
- **El muestreo es una grilla regular** —20 min para HR/RR/SpO2, 30 para wearables, 60 para TEMP,
  120 para SBP/DBP— y no hay celdas vacías en ningún archivo. La cobertura de una ventana es un
  conteo exacto contra lo esperado, no una estimación.
- **Todos los antecedentes se registraron antes del 2026-07-01**, el inicio de la ventana de
  estudio: no introducen riesgo de leakage.

## 10 · Limitaciones conocidas

- **No hay validación contra Gold Standard.** Las métricas de falsas alertas y anticipación se
  calculan contra un conjunto de negativos derivado de marcadores públicos y una ventana de
  oportunidad definida por el equipo. Están explicadas, pero no son equivalentes a una evaluación
  ciega.
- **La calibración proviene de una sola cohorte.** El dataset se declara
  `Candidate 1 — not yet frozen final release`. Todas las constantes viven en
  `config/scoring.yaml`: recalibrar es correr un script, no reescribir el motor.
- **El recorte por canal no se puso a prueba en estos datos.** La ablación lo muestra: en RISA una
  variable aislada no llega al umbral de HIGH ni sin la protección.
- **SBP y DBP aportan poco.** Con muestreo de 120 minutos hay tres muestras en una ventana de seis
  horas: se calcula nivel pero no pendiente, y su peso es la mitad.
- **A las 09:00 la trayectoria oficial no dispara.** Con ruido realista, una hora de rampa suave no
  es distinguible del baseline. La ganancia honesta es de una hora, no de dos.
- **La ventana de evidencia es fija en 6 horas.** Un deterioro más lento que eso se detecta tarde;
  una ventana adaptativa por canal es trabajo pendiente.
- **Las primeras 54 horas de cada encuentro no se evalúan.** Es lo que hace falta para tener un
  baseline diurno completo, y acortarlo empeora todo (ver la auditoría). Un deterioro que empieza
  en ese tramo se detecta al primer instante evaluable, no antes.
- **La ingesta reconstruye entera, no carga sólo lo nuevo.** El sistema sí sabe qué filas son
  nuevas —`manifest.clasificar` devuelve `APENDADO` comparando el hash del prefijo—, pero cargar
  sólo esas sería incorrecto: `mark_duplicates` desempata cada grupo por `available_time`, y ese
  campo se lo reescribe a las retransmisiones el paso que lee las ventanas de
  `connectivity_events.csv`. Agregar filas a ese archivo cambia el `is_duplicate` de filas de
  `vital_signs` ya cargadas, cuyo propio archivo no se tocó (37 de las 540 retransmisiones caen
  justo en el borde de una ventana). Lo mismo con `is_plausible`, que es columna guardada contra
  los límites de `variable_catalog.csv`. Hacerlo incremental exige un grafo de invalidación entre
  archivos y versionado de reglas, y a cambio ahorraría ~33 s de los 39 que tarda reconstruir todo.
  A esta escala no compensa, y cuesta el determinismo de P-05: la capa limpia pasaría a depender
  del historial de ejecuciones y no sólo de los datos.
- **No hay control de acceso.** La API es local y de sólo lectura; un despliegue real necesitaría
  autenticación y autorización por rol.
- **Alcance clínico.** El sistema señala situaciones que ameritan revisión. No diagnostica, no
  pronostica y no recomienda conductas.

## 11 · Declaración de tecnologías y componentes externos

| Categoría | Uso |
|---|---|
| Modelos preentrenados o fundacionales | **Ninguno** |
| APIs externas | **Ninguna** |
| Servicios cloud | **Ninguno**. Todo corre local |
| Datasets complementarios | **Ninguno**. Sólo RISA Data V1.0 |
| Librerías | DuckDB, NumPy, PyYAML, FastAPI, Uvicorn, pytest, httpx — todas open source |
| Herramientas de IA generativa | **Claude Code (Anthropic)** como asistente de programación y análisis durante el desarrollo |

**Ningún modelo generativo participa en el cálculo de puntajes, la asignación de prioridad ni la
selección de evidencia.** El motor es determinista: la misma entrada produce la misma salida, y
hay pruebas que lo verifican. Toda alerta, valoración y explicación se sustenta en registros
concretos de RISA identificados por `source_file` y `record_id`.
