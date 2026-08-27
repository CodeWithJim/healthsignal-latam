# Guion del pitch — HealthSignal LATAM

**3:30 de pitch + demostración funcional · 3 min de preguntas del jurado**

La demostración es el componente central: el jurado valora especialmente poder comprobar
directamente cómo la solución identifica una situación y qué la sustenta. Todo lo que sigue está
cronometrado para hablarse, no para leerse.

## Antes de empezar

```bash
.venv\Scripts\python.exe scripts\05_serve.py
```

Tres cosas abiertas y listas:

| Pestaña | Estado inicial |
|---|---|
| **1 · Interfaz** `http://127.0.0.1:8000/` | Ranking cargado, filtro en CRITICAL |
| **2 · Terminal** | En la raíz del repo, listo para `scripts\06_caso.py` |
| **3 · Diagrama** | `ARCHITECTURE.md` §1 abierto |

Verificación de 30 segundos antes de entrar: la interfaz carga, el ranking tiene **22 CRITICAL**, la
primera es `HS-0869-20260720T1800` con riesgo 0,930, y `/decide` responde.

---

## 1 · Problema y propuesta — 30 s

> RISA no tiene un problema de falta de datos: tiene dos millones y medio de registros que llegan
> de cinco fuentes, a frecuencias distintas, y **en momentos distintos de cuando ocurrieron**.
>
> Nuestra tesis es que la señal no está en el valor alto. Está en **varios canales moviéndose
> juntos, en la dirección coherente, de forma sostenida** — contra el historial del propio paciente.
>
> Y el diferenciador: cada señal viaja con los registros exactos que la sustentan, **y con las
> hipótesis alternativas que evaluamos y descartamos**.

*Pantalla: interfaz con el ranking.*

## 2 · Arquitectura y enfoque técnico — 1 min

*Pantalla: pestaña 3, el diagrama.*

> Pipeline vectorizado abajo, núcleo de decisión aislado arriba. La frontera se cruza una sola vez.
>
> **Esta caja es la decisión de ingeniería que define el sistema.** El motor no lee la base: lee
> `snapshot(paciente, T)`, que nunca devuelve un hecho con `available_time` posterior a T. Mientras
> no tenga otra vía de lectura, el temporal leakage no es un bug que haya que buscar: es
> inexpresable.
>
> No usamos modelo entrenado, y es una decisión defendible: no hay etiquetas públicas, un detector
> no supervisado es una fábrica de falsas alarmas, y un score no es evidencia. La innovación está
> en el método.
>
> Tres medidas por canal contra su propio baseline: nivel, pendiente robusta y persistencia. Y el
> mecanismo entero es **el recorte por canal antes de agregar**: hace que cuatro canales moderados
> superen a uno extremo por construcción, no por ajuste de umbrales.
>
> El contrato de salida vive como restricciones de base de datos. Evidencia huérfana es imposible
> de insertar. Pasar el validador oficial no es una tarea: es una consecuencia del esquema.

## 3 · Demostración funcional — 1 min

**Es el componente central. Hacer, no narrar.** Tres golpes: la señal, la que *no* alertó, y el
instante que elige el jurado.

### a) La señal · `HS-0869-20260720T1800` — 20 s

*Pestaña 1, filtro CRITICAL, clic en la primera.*

> Paciente 869. Seis horas: saturación **−11,6 desviaciones** contra su propio baseline, respiratoria
> +7,3, temperatura +5,8, cardíaca +4,7. Cuatro canales, todos sostenidos al 100 %.
>
> La franja sombreada es la ventana de evidencia. Y cada fila de abajo apunta a un registro real.

*Clic en un `record_id`. Se abre la fila original.*

> Ésa es la fila tal cual está en `vital_signs.csv`. De la alerta al dato de origen, en un clic.

### b) La que NO alertó · `HS-0271-20260717T1200` — 25 s

**Éste es el momento que diferencia la propuesta.** *Filtro LOW, clic.*

> Miren esta otra. Frecuencia cardíaca **+5,4 desviaciones**, respiratoria +3,0, ambas sostenidas al
> 100 %. Con umbrales, esto alerta.
>
> Nosotros la dejamos en **0,149 y prioridad LOW** —*señalar el bloque de reglas*— porque había
> contexto de actividad física y la desviación está dominada por la frecuencia cardíaca: aporta el
> 59 % del puntaje. La regla bajó el puntaje un 70 % **y citó `CTX-0002408`**, el intervalo que lo
> justifica.
>
> No la descartamos en silencio: la emitimos igual, con el motivo escrito. Ésa es nuestra prueba de
> control de falsas alertas, y está en el CSV de entrega.

### c) El instante que nadie preparó — 15 s

*Formulario de arriba.*

> Ahora lo que no está preparado. **Denme un paciente y una hora.**

*Escribir lo que diga el jurado. Enter.*

> Computado en el momento, con la garantía temporal. Nada precargado.

**Si el jurado no propone nada**, el contraste ensayado —el mismo paciente, nueve horas de
diferencia:

| | `PAT-0009` |
|---|---|
| `2026-07-12T09:00` | riesgo **0,000** · LOW · 0 canales |
| `2026-07-12T18:00` | riesgo **0,927** · CRITICAL · 4 canales |

> La misma persona. Nueve horas. **Es una decisión, no una consulta a una tabla.**

### Casos de respaldo

Verificados contra la API el 2026-08-27.

| Para mostrar | Señal | Resultado | Cita |
|---|---|---|---|
| Deterioro limpio de 4 canales | `HS-0869-20260720T1800` | 0,930 CRITICAL · 18 evidencias | — |
| Contexto evaluado que **no** explica | `HS-0992-20260710T1345` | 0,912 CRITICAL · `actividad_evaluada` | `context_id` |
| Suprimida por actividad | `HS-0271-20260717T1200` | 0,149 LOW · −70 % | `CTX-0002408` |
| Evidencia `QUALITY` citada | `HS-0776-20260722T1900` | 0,551 MEDIUM | `OBS-0001273608` |

## 4 · Resultados y métricas — 30 s

> 95.731 evaluaciones sobre los mil pacientes, seis minutos. **210 señales, no noventa y cinco mil**:
> evaluamos cada hora pero emitimos sólo en cambio material de estado.
>
> Cero violaciones de causalidad temporal en 3.405 filas de evidencia. Cero señales sin evidencia,
> cero huérfanas. Validador oficial sin errores ni warnings.
>
> Anticipación mediana de cuatro horas hasta el máximo posterior. Y sobre falsas alertas: **cero por
> ciento de impacto en distractores** sobre 56 señales altas, medido contra un conjunto de negativos
> que construimos desde los marcadores que el propio escenario declara, porque el Gold Standard es
> privado.
>
> La ablación dice qué compra cada pieza: la persistencia elimina el 29 % de las señales **sin perder
> una sola detección**, y la concordancia sube las detecciones de 13 a 20 sin perder anticipación.

## 5 · Impacto y evolución — 30 s

> Incorporar una fuente nueva es **agregar una declaración a un YAML**, no tocar el pipeline.
>
> El sistema no asume conectividad uniforme: la cobertura de cada ventana es un conteo exacto, y su
> degradación baja la **confianza**, nunca sube el riesgo. Un paciente desconectado no está menos
> grave por estar desconectado: está peor observado.
>
> Y el alcance está declarado: esto señala a quién revisar primero y por qué. No diagnostica.

---

## Preguntas del jurado — respuestas preparadas

**¿Por qué no usaron machine learning?**
No hay etiquetas públicas. Supervisado es imposible; no supervisado es un detector de anomalías, o
sea una fábrica de falsas alarmas. Y la guía penaliza explícitamente generar alertas con IA sin
evidencia rastreable. Un score de 0.92 ordena, pero no es evidencia.

**¿Cómo garantizan que no usan información futura?**
Por tres capas. La cláusula SQL, el recorte del historial, y el constructor del snapshot que lanza
excepción si algo se coló. Además hay un test que toma un laboratorio con muestra antes de T e
informe después, y verifica que no aparece. Y la tabla declara `CHECK (available_time >= event_time)`.

**¿Y las formas sutiles de leakage?**
Dos que atacamos explícitamente. El baseline nunca se solapa con la ventana de evidencia, así que
el evento no contamina su propia referencia. Y un intervalo en curso no revela su final: saber que
el sueño termina a las 06:00 cuando son las 01:00 es futuro, así que lo recortamos en T.

**¿Cómo controlan las falsas alertas?**
Cuatro reglas de supresión, cada una emite su propia fila de evidencia con el `record_id` que la
justifica. Y cuando el contexto está presente pero no explica el patrón, **lo registramos igual**:
haber descartado una hipótesis es una decisión, y no se toma en silencio.

**¿Por qué 210 señales y no más?**
Porque más alertas no es mejor desempeño. Evaluamos 95.731 veces y emitimos en cambio material de
banda, con refractario de 12 horas. Un paciente que escala de MEDIUM a CRITICAL genera tres señales
en tres horas, y eso sí es información.

**¿Cómo calibraron los umbrales?**
Contra la distribución observada, no a ojo. Los pisos de dispersión salen del percentil 5 de la MAD
poblacional. Todo vive en `config/scoring.yaml`, y el dataset se declara *Candidate 1*: recalibrar
es correr un script.

**¿Qué pasa si un paciente se desconecta?**
Baja la confianza, no sube el riesgo, y queda con tope de prioridad. El evento de conectividad se
cita como evidencia `QUALITY`. Riesgo alto con confianza baja lo comunicamos como **solicitud de
verificación**, no como alerta clínica.

**¿Usaron IA generativa?**
Claude Code como asistente de desarrollo, declarado en el README. **Ningún modelo generativo
participa en el cálculo de puntajes, la prioridad ni la selección de evidencia.** El motor es
determinista: hay un test que corre la misma entrada dos veces y compara.

**¿Seguridad?**
La API es de sólo lectura y no expone SQL. El acceso a las filas originales va contra lista blanca:
la ruta nunca se construye con texto libre del pedido, y hay un test que intenta travesía de
directorios. Cero credenciales en el código.

**¿Qué limitaciones reconocen?**
Cuatro. Las métricas de falsas alertas se calculan contra un conjunto de negativos derivado de
marcadores públicos, no contra un Gold Standard. La calibración viene de una sola cohorte. Las
primeras 54 horas de cada encuentro no se evalúan porque hace falta un baseline diurno completo —
lo medimos, acortarlo empeora todo. Y la ventana de evidencia es fija en seis horas: un deterioro
más lento se detecta tarde.

**¿Escalaría a una red real?**
El cuello de botella es por paciente y es paralelizable: seis minutos para mil pacientes en un solo
proceso. El modelo de datos ya separa cuándo ocurrió de cuándo estuvo disponible, que es lo que
rompe la mayoría de las integraciones reales. Lo que faltaría es control de acceso por rol e
ingesta incremental en vez de barrido completo.

---

## Reglas de la demostración

1. Lo que no se pueda demostrar cuenta como funcionalidad proyectada, no implementada.
2. Las diapositivas no sustituyen la demostración funcional.
3. Toda señal que se muestre tiene que poder relacionarse con la evidencia que la originó.
4. Las salidas se presentan como apoyo, nunca como diagnóstico.

**Si algo falla en vivo:** `scripts\06_caso.py` imprime la cadena completa en terminal sin depender
del servidor, y `results/signals.csv` está en disco. El validador oficial se puede correr delante
del jurado en diez segundos.
