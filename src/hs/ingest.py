"""Ingesta y homogeneización de RISA Data V1.0 (etapas 1-2 del pipeline).

Los adaptadores de config/sources.yaml declaran el mapeo; este módulo genera el
SQL y DuckDB lo ejecuta. Agregar una fuente no requiere tocar este archivo (RF-01).

Invariante que sostiene RF-02:  rows_read = rows_loaded + rows_quarantined.

Criterio de carga: una fila se carga si es interpretable. Las filas implausibles
o duplicadas SÍ se cargan, marcadas con su flag, para que sigan siendo citables
como evidencia QUALITY (P-04). La cuarentena guarda sólo lo que no pudo leerse.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
import yaml

from . import manifest, paths

# Motivos de cuarentena, en orden de evaluación.
QUARANTINE_REASONS = (
    "NO_RECORD_ID", "NO_PATIENT", "BAD_EVENT_TIME",
    "BAD_AVAILABLE_TIME", "TIME_ORDER", "UNIT_UNKNOWN", "NO_VALUE",
)

# Tablas que cada corrida reconstruye. `ingest_manifest` NO está: es un registro
# acumulativo, no un estado. Borrarla en cada corrida destruiría justamente lo
# que la hace útil — poder responder si el contenido de una fuente cambió entre
# dos ingestas, que es la pregunta que el hash existe para contestar.
CLEAN_TABLES = ("observations", "intervals", "clinical_facts", "quarantine")

# Las salidas también se descartan: quedaron calculadas sobre la capa limpia
# anterior, y dejarlas junto a una capa reconstruida es la forma más fácil de
# entregar señales que no corresponden a los datos. `evidence` va primero por su
# clave foránea contra `signals`.
RESULT_TABLES = ("evidence", "signals")


# --------------------------------------------------------------------------- helpers

def _col(name: str | None) -> str:
    """Referencia a columna del CSV, o NULL si el adaptador no la declara."""
    return "NULL" if not name else f'"{name}"'


def _csv(source_file: str) -> str:
    p = paths.sql_literal(paths.raw_path(source_file))
    return f"read_csv('{p}', header=true, all_varchar=true)"


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def load_config() -> dict[str, Any]:
    with open(paths.SOURCES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def all_source_files(cfg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for section in ("catalogs", "dimensions", "observations", "intervals", "clinical_facts"):
        out += [a["source_file"] for a in cfg.get(section, [])]
    return out


def connect(path=None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path or paths.WAREHOUSE))
    con.execute(paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    return con


# --------------------------------------------------------------------------- etapas

def load_catalogs_and_dimensions(con, cfg) -> dict[str, int]:
    """Espejo 1:1 de catálogos y dimensiones. Son el contrato semántico de la data."""
    counts = {}
    for adapter in cfg.get("catalogs", []) + cfg.get("dimensions", []):
        table, sf = adapter["table"], adapter["source_file"]
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {_csv(sf)}")
        counts[sf] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


def stage_observations(con, cfg) -> dict[str, int]:
    """Vuelca las 4 fuentes de observaciones a una tabla intermedia de texto plano."""
    con.execute("""
        CREATE OR REPLACE TABLE _obs_stage (
            source_file VARCHAR, record_id VARCHAR, patient_id VARCHAR,
            encounter_id VARCHAR, device_id VARCHAR, variable_code VARCHAR,
            domain VARCHAR, event_time_raw VARCHAR, available_time_raw VARCHAR,
            value_raw_text VARCHAR, unit_raw VARCHAR, quality_flag VARCHAR,
            source_system VARCHAR, ref_low_raw VARCHAR, ref_high_raw VARCHAR
        )
    """)
    read = {}
    for a in cfg.get("observations", []):
        sf = a["source_file"]
        con.execute(f"""
            INSERT INTO _obs_stage
            SELECT {_lit(sf)}, {_col(a['record_id'])}, {_col(a['patient'])},
                   {_col(a.get('encounter'))}, {_col(a.get('device'))}, {_col(a['variable'])},
                   {_lit(a['domain'])}, {_col(a['event_time'])}, {_col(a['available_time'])},
                   {_col(a['value'])}, {_col(a['unit'])}, {_col(a.get('quality_flag'))},
                   {_col(a.get('source_system'))}, {_col(a.get('ref_low'))}, {_col(a.get('ref_high'))}
            FROM {_csv(sf)}
        """)
        read[sf] = con.execute(
            "SELECT count(*) FROM _obs_stage WHERE source_file = ?", [sf]
        ).fetchone()[0]
    return read


def split_observations(con) -> None:
    """Parsea, convierte unidades, aplica plausibilidad y separa carga de cuarentena.

    La conversión usa units_catalog (RD-04) y la plausibilidad variable_catalog
    (RD-06). quality_flag del origen se conserva pero NO decide nada.
    """
    con.execute(f"""
        CREATE OR REPLACE TABLE _obs_typed AS
        WITH parsed AS (
            SELECT s.*,
                   TRY_CAST(s.event_time_raw     AS TIMESTAMP) AS et,
                   TRY_CAST(s.available_time_raw AS TIMESTAMP) AS av,
                   TRY_CAST(s.value_raw_text     AS DOUBLE)    AS vnum,
                   TRY_CAST(u.conversion_factor  AS DOUBLE)    AS cf,
                   TRY_CAST(u.conversion_offset  AS DOUBLE)    AS co,
                   u.canonical_unit                            AS cu,
                   TRY_CAST(v.plausibility_min   AS DOUBLE)    AS pmin,
                   TRY_CAST(v.plausibility_max   AS DOUBLE)    AS pmax
            FROM _obs_stage s
            LEFT JOIN units_catalog    u ON u.unit_code     = s.unit_raw
            LEFT JOIN variable_catalog v ON v.variable_code = s.variable_code
        )
        SELECT *,
               CASE WHEN vnum IS NOT NULL AND cf IS NOT NULL
                    THEN vnum * cf + coalesce(co, 0) END AS vcanon,
               CASE
                 WHEN record_id  IS NULL OR trim(record_id)  = '' THEN 'NO_RECORD_ID'
                 WHEN patient_id IS NULL OR trim(patient_id) = '' THEN 'NO_PATIENT'
                 WHEN et IS NULL                                  THEN 'BAD_EVENT_TIME'
                 WHEN av IS NULL                                  THEN 'BAD_AVAILABLE_TIME'
                 WHEN av <  et                                    THEN 'TIME_ORDER'
                 WHEN cu IS NULL                                  THEN 'UNIT_UNKNOWN'
                 WHEN vnum IS NULL AND (value_raw_text IS NULL
                                        OR trim(value_raw_text) = '') THEN 'NO_VALUE'
                 ELSE NULL
               END AS reject_reason
        FROM parsed
    """)

    con.execute("""
        INSERT INTO observations
        SELECT source_file, record_id, patient_id,
               nullif(trim(coalesce(encounter_id, '')), ''),
               nullif(trim(coalesce(device_id, '')), ''),
               variable_code, domain, et, av,
               vcanon,
               CASE WHEN vnum IS NULL THEN value_raw_text END,
               vnum, unit_raw, cu,
               nullif(trim(coalesce(source_system, '')), ''),
               nullif(trim(coalesce(quality_flag, '')), ''),
               CASE
                 WHEN vnum IS NULL                    THEN TRUE   -- categórico
                 WHEN pmin IS NULL OR pmax IS NULL    THEN TRUE   -- sin límites declarados
                 ELSE vcanon BETWEEN pmin AND pmax
               END,
               FALSE,
               TRY_CAST(ref_low_raw AS DOUBLE), TRY_CAST(ref_high_raw AS DOUBLE),
               TRUE
        FROM _obs_typed
        WHERE reject_reason IS NULL
    """)

    con.execute("""
        INSERT INTO quarantine
        SELECT source_file, record_id, patient_id, variable_code, reject_reason,
               concat_ws(' | ', 'event=' || coalesce(event_time_raw, '<null>'),
                                'available=' || coalesce(available_time_raw, '<null>'),
                                'value=' || coalesce(value_raw_text, '<null>'),
                                'unit=' || coalesce(unit_raw, '<null>')),
               NULL
        FROM _obs_typed
        WHERE reject_reason IS NOT NULL
    """)


def load_intervals(con, cfg) -> dict[str, int]:
    read = {}
    for a in cfg.get("intervals", []):
        sf = a["source_file"]
        extra = a.get("extra") or []
        extra_sql = (
            "to_json({" + ", ".join(f"{_lit(c)}: {_col(c)}" for c in extra) + "})"
            if extra else "NULL"
        )
        con.execute(f"""
            CREATE OR REPLACE TABLE _int_stage AS
            SELECT {_lit(sf)} AS source_file, {_col(a['record_id'])} AS record_id,
                   {_col(a['patient'])} AS patient_id, {_col(a.get('device'))} AS device_id,
                   {_lit(a['kind'])} AS kind, {_col(a.get('subtype'))} AS subtype,
                   {_col(a.get('value_text'))} AS value_text,
                   TRY_CAST({_col(a['start_time'])}     AS TIMESTAMP) AS start_time,
                   TRY_CAST({_col(a['end_time'])}       AS TIMESTAMP) AS end_time,
                   TRY_CAST({_col(a['available_time'])} AS TIMESTAMP) AS available_time,
                   TRY_CAST({_col(a.get('confidence'))} AS DOUBLE)    AS confidence,
                   {extra_sql} AS extra_json
            FROM {_csv(sf)}
        """)
        read[sf] = con.execute("SELECT count(*) FROM _int_stage").fetchone()[0]

        con.execute("""
            INSERT INTO intervals
            SELECT source_file, record_id, patient_id, device_id, kind, subtype, value_text,
                   start_time, end_time, available_time, confidence, extra_json
            FROM _int_stage
            WHERE record_id IS NOT NULL AND trim(record_id) <> ''
              AND patient_id IS NOT NULL AND trim(patient_id) <> ''
              AND start_time IS NOT NULL AND end_time IS NOT NULL
              AND available_time IS NOT NULL AND end_time >= start_time
        """)
        con.execute("""
            INSERT INTO quarantine
            SELECT source_file, record_id, patient_id, NULL,
                   CASE
                     WHEN record_id IS NULL OR trim(record_id) = ''   THEN 'NO_RECORD_ID'
                     WHEN patient_id IS NULL OR trim(patient_id) = '' THEN 'NO_PATIENT'
                     WHEN start_time IS NULL                          THEN 'BAD_EVENT_TIME'
                     WHEN available_time IS NULL                      THEN 'BAD_AVAILABLE_TIME'
                     ELSE 'TIME_ORDER'
                   END,
                   'interval', NULL
            FROM _int_stage
            WHERE NOT (record_id IS NOT NULL AND trim(record_id) <> ''
                   AND patient_id IS NOT NULL AND trim(patient_id) <> ''
                   AND start_time IS NOT NULL AND end_time IS NOT NULL
                   AND available_time IS NOT NULL AND end_time >= start_time)
        """)
    return read


def load_clinical_facts(con, cfg) -> dict[str, int]:
    read = {}
    for a in cfg.get("clinical_facts", []):
        sf = a["source_file"]
        con.execute(f"""
            INSERT INTO clinical_facts
            SELECT {_lit(sf)}, {_col(a['record_id'])}, {_col(a['patient'])},
                   {_col(a.get('category'))},
                   TRY_CAST({_col(a['onset'])} AS DATE),
                   TRY_CAST({_col(a['available_time'])} AS TIMESTAMP),
                   {_col(a.get('status'))}, {_col(a.get('severity'))},
                   {_col(a.get('source_system'))}
            FROM {_csv(sf)}
            WHERE TRY_CAST({_col(a['available_time'])} AS TIMESTAMP) IS NOT NULL
        """)
        read[sf] = con.execute(f"SELECT count(*) FROM {_csv(sf)}").fetchone()[0]
    return read


def resolver_disponibilidad_retransmitidas(con) -> tuple[int, int]:
    """Una observación retransmitida no estuvo disponible en su timestamp.

    Los signos vitales no traen columna de disponibilidad: se deriva. La regla
    oficial dice que valen desde su `timestamp` «salvo otra lógica documentada»,
    y para las filas de MONITOR_RETRANSMIT esa regla por defecto es falsa: el
    canal declara latencia DELAYED y la fila llegó cuando la red se restableció.

    Se acota con el cierre de la ventana de conectividad que la contiene. No es
    una estimación sino una cota: durante el corte no pudo llegar; al cerrarse,
    sí. Sólo mueve hacia adelante.

    Si no hay ventana que la contenga no se puede acotar nada, y dejarla en su
    `timestamp` sería afirmar una disponibilidad que sabemos falsa. Esas filas
    quedan marcadas: fuera del cálculo, pero en la tabla y citables.

    Devuelve (filas ajustadas, filas sin ventana).
    """
    ajustadas = con.execute("""
        UPDATE observations AS o
        SET available_time = c.end_time
        FROM intervals AS c
        WHERE o.source_system = 'MONITOR_RETRANSMIT'
          AND c.kind = 'CONNECTIVITY'
          AND c.patient_id = o.patient_id
          AND o.event_time BETWEEN c.start_time AND c.end_time
          AND c.end_time > o.available_time
    """).fetchone()[0]

    sin_ventana = con.execute("""
        UPDATE observations AS o
        SET is_availability_known = FALSE
        WHERE o.source_system = 'MONITOR_RETRANSMIT'
          AND NOT EXISTS (
                SELECT 1 FROM intervals AS c
                WHERE c.kind = 'CONNECTIVITY'
                  AND c.patient_id = o.patient_id
                  AND o.event_time BETWEEN c.start_time AND c.end_time)
    """).fetchone()[0]

    return ajustadas, sin_ventana


def mark_duplicates(con) -> int:
    """Marca como duplicada toda observación que repite (paciente, variable, evento).

    Se conserva la que estuvo disponible primero. El segundo criterio no es
    cosmético: 37 de las 540 retransmisiones caen justo en el borde de cierre de
    su ventana de conectividad, donde el ajuste anterior deja lag cero y empatan
    con la fila del gateway. Preferir explícitamente la no retransmitida evita
    que la corrección dependa del orden de los identificadores.
    """
    return con.execute("""
        UPDATE observations AS o
        SET is_duplicate = TRUE
        FROM (
            SELECT source_file, record_id,
                   row_number() OVER (
                       PARTITION BY patient_id, variable_code, event_time
                       ORDER BY available_time ASC,
                                (coalesce(source_system, '') = 'MONITOR_RETRANSMIT') ASC,
                                record_id ASC
                   ) AS rn
            FROM observations
        ) AS d
        WHERE o.source_file = d.source_file AND o.record_id = d.record_id AND d.rn > 1
    """).fetchone()[0]


def reconstruir_tablas(con) -> None:
    """Descarta la capa limpia y las salidas, y las vuelve a crear vacías.

    Se hace con DROP y no con DELETE. Medido sobre 2.536.442 filas el
    2026-08-27: `DELETE FROM` tarda 127 s y `DROP` + recrear desde el esquema
    tarda 0,0 s — el 75 % del tiempo de la corrida se iba vaciando tablas que
    se vuelven a llenar enseguida.

    Recrear desde `schema.sql` devuelve las tablas con sus CHECK y su clave
    foránea intactos; hay pruebas que intentan violarlos y esperan la excepción.
    """
    for t in RESULT_TABLES + CLEAN_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.execute(paths.SCHEMA_SQL.read_text(encoding="utf-8"))


def descartar_exportaciones(destino=None) -> list[str]:
    """Borra results/*.csv: se calcularon sobre la capa limpia anterior.

    Dejarlos junto a una capa reconstruida es la forma más fácil de entregar
    señales que no corresponden a los datos cargados. Se regeneran con
    scripts/02_detect.py.
    """
    destino = destino or paths.RESULTS
    borrados = []
    for nombre in ("signals.csv", "evidence.csv"):
        p = destino / nombre
        if p.exists():
            p.unlink()
            borrados.append(nombre)
    return borrados


def estado_previo(con) -> dict[str, tuple[int, str]]:
    """Con qué bytes y qué hash se ingestó cada fuente la última vez.

    Es la referencia contra la que se decide si un archivo creció legítimamente
    o si le editaron algo ya procesado. Por eso `ingest_manifest` es acumulativa
    y no se borra entre corridas.
    """
    filas = con.execute("""
        SELECT source_file, bytes, sha256 FROM ingest_manifest
        WHERE run_id = (SELECT run_id FROM ingest_manifest
                        ORDER BY ingested_at DESC LIMIT 1)
    """).fetchall()
    return {sf: (int(b), sha) for sf, b, sha in filas}


def write_manifest(con, cfg, run_id: str, integrity: dict, rows_read: dict) -> None:
    loaded = dict(con.execute(
        "SELECT source_file, count(*) FROM observations GROUP BY 1"
    ).fetchall())
    loaded.update(dict(con.execute(
        "SELECT source_file, count(*) FROM intervals GROUP BY 1"
    ).fetchall()))
    loaded.update(dict(con.execute(
        "SELECT source_file, count(*) FROM clinical_facts GROUP BY 1"
    ).fetchall()))
    quar = dict(con.execute(
        "SELECT source_file, count(*) FROM quarantine GROUP BY 1"
    ).fetchall())

    targets = {}
    for a in cfg.get("catalogs", []) + cfg.get("dimensions", []):
        targets[a["source_file"]] = a["table"]
        loaded[a["source_file"]] = con.execute(
            f"SELECT count(*) FROM {a['table']}"
        ).fetchone()[0]
    for a in cfg.get("observations", []):
        targets[a["source_file"]] = "observations"
    for a in cfg.get("intervals", []):
        targets[a["source_file"]] = "intervals"
    for a in cfg.get("clinical_facts", []):
        targets[a["source_file"]] = "clinical_facts"

    now = dt.datetime.now().replace(microsecond=0)
    sha = manifest.git_sha()
    for sf, info in integrity.items():
        con.execute(
            "INSERT INTO ingest_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, sf, info["sha256"], info["sha256_expected"], info["sha256_ok"],
             info["bytes"], rows_read.get(sf, 0), loaded.get(sf, 0), quar.get(sf, 0),
             targets.get(sf), now, sha, info["estado"]],
        )


def run(con=None, verbose: bool = True) -> dict[str, Any]:
    """Ejecuta la ingesta completa. Idempotente: reconstruye la capa CLEAN."""
    log = print if verbose else (lambda *a, **k: None)
    cfg = load_config()
    own = con is None
    con = con or connect()

    try:
        run_id = "ing-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        log(f"run_id = {run_id}")

        log("Verificando integridad de las fuentes ...")
        # Se calcula todo SIN abortar todavía: si algo cambió conviene decir
        # también respecto de qué, antes de detener el proceso. Un fallo sin
        # diagnóstico obliga a investigar a mano lo que el sistema ya sabe.
        previo = estado_previo(con)
        integrity = manifest.verify(all_source_files(cfg), previo, strict=False)

        por_estado: dict[str, list[str]] = {}
        for sf, v in integrity.items():
            por_estado.setdefault(v["estado"], []).append(sf)

        coinciden = sum(1 for v in integrity.values() if v["sha256_ok"])
        log(f"  {coinciden}/{len(integrity)} coinciden con MANIFEST_SHA256.txt")
        if previo:
            resumen = "  ".join(f"{e}: {len(v)}" for e, v in sorted(por_estado.items()))
            log(f"  respecto de la ingesta anterior -> {resumen}")

        apendadas = por_estado.get(manifest.APENDADO, [])
        for sf in apendadas:
            crecio = integrity[sf]["bytes"] - previo[sf][0]
            log(f"  INFORMACIÓN NUEVA en {sf}: +{crecio:,} bytes, "
                f"lo anterior intacto — se procesa")

        versiones = por_estado.get(manifest.NUEVA_VERSION, [])
        if versiones:
            log(f"  VERSIÓN NUEVA en {len(versiones)} fuente(s): el contenido cambió por")
            log("    completo pero coincide con MANIFEST_SHA256.txt, así que es una entrega")
            log("    nueva de la organización y no una edición. Se procesa.")
            for sf in versiones[:5]:
                log(f"      {sf}")

        alteradas = por_estado.get(manifest.MODIFICADO, [])
        if alteradas:
            log("\n  Se editó contenido que este sistema ya había procesado. No es")
            log("  información nueva: las filas anteriores cambiaron. El proceso se")
            log("  detiene antes de leer nada, para no mezclar resultados con datos")
            log("  cuya procedencia ya no se puede afirmar.")
            raise manifest.IntegrityError(
                "Contenido ya procesado fue alterado en: " + ", ".join(alteradas))

        sospechosas = manifest.primera_ingesta_valida(integrity)
        if sospechosas:
            log("\n  Primera ingesta de fuentes que el manifiesto oficial lista pero cuyo")
            log("  contenido no coincide. Si es una versión nueva del dataset, reemplazar")
            log("  también MANIFEST_SHA256.txt: el manifiesto viene con los datos.")
            raise manifest.IntegrityError(
                "No coinciden con MANIFEST_SHA256.txt: " + ", ".join(sospechosas))

        reconstruir_tablas(con)
        borrados = descartar_exportaciones()
        log(f"Capa CLEAN y salidas reconstruidas"
            + (f"; descartado results/{', results/'.join(borrados)}" if borrados else ""))

        log("Cargando catálogos y dimensiones ...")
        rows_read = load_catalogs_and_dimensions(con, cfg)

        log("Preparando observaciones ...")
        rows_read.update(stage_observations(con, cfg))
        split_observations(con)

        log("Cargando intervalos ...")
        rows_read.update(load_intervals(con, cfg))

        log("Cargando hechos clínicos ...")
        rows_read.update(load_clinical_facts(con, cfg))

        n_adj, n_sin = resolver_disponibilidad_retransmitidas(con)
        log(f"Disponibilidad acotada por retransmisión: {n_adj} filas")
        if n_sin:
            log(f"  {n_sin} retransmitida(s) sin ventana de conectividad que las acote:")
            log("    marcadas is_availability_known = FALSE. Quedan fuera del cálculo")
            log("    porque usarlas obligaría a afirmar una disponibilidad indefendible.")

        n_dup = mark_duplicates(con)
        log(f"Marcadas como duplicadas: {n_dup} filas")

        write_manifest(con, cfg, run_id, integrity, rows_read)
        corridas = con.execute(
            "SELECT count(DISTINCT run_id) FROM ingest_manifest").fetchone()[0]
        log(f"Registro de ingestas: {corridas} corrida(s) acumulada(s)")
        con.execute("DROP TABLE IF EXISTS _obs_stage")
        con.execute("DROP TABLE IF EXISTS _obs_typed")
        con.execute("DROP TABLE IF EXISTS _int_stage")

        return {"run_id": run_id, "integrity": integrity, "adjusted": n_adj,
                "sin_ventana": n_sin, "duplicates": n_dup}
    finally:
        if own:
            con.close()
