"""Criterios de aceptación de la Fase 0 (RAW -> CLEAN).

Requieren que `scripts/00_ingest.py` haya corrido. Verifican contra el almacén
construido, no contra los CSV: lo que se prueba es lo que el motor va a leer.

Las cifras esperadas provienen del perfilado del 2026-08-26 sobre
RISA Data V1.0 Candidate 1, con los 17 archivos coincidiendo con el manifiesto.
"""
from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from hs import ingest, paths
from hs.timeline import AsOfStore

STUDY_START = dt.datetime(2026, 7, 1, 0, 0, 0)
STUDY_END = dt.datetime(2026, 7, 31, 8, 0, 0)


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("Falta el almacén: correr scripts/00_ingest.py")
    c = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    yield c
    c.close()


def one(con, sql, *args):
    return con.execute(sql, list(args)).fetchone()[0]


# --------------------------------------------------------------- integridad

def test_las_17_fuentes_coinciden_con_el_manifiesto(con):
    """P-08 / RNF-04. Acotado a la última corrida: el registro es acumulativo."""
    total, ok = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE sha256_ok) FROM ingest_manifest
        WHERE run_id = (SELECT max(run_id) FROM ingest_manifest)
    """).fetchone()
    assert total == 17, f"se esperaban 17 fuentes, hay {total}"
    assert ok == 17, f"{total - ok} archivos no coinciden con MANIFEST_SHA256.txt"


def test_el_registro_de_ingestas_es_acumulativo(con):
    """Sin historial no se puede saber si una fuente creció o si la editaron."""
    corridas = con.execute(
        "SELECT count(DISTINCT run_id) FROM ingest_manifest").fetchone()[0]
    assert corridas >= 1
    sin_estado = con.execute(
        "SELECT count(*) FROM ingest_manifest WHERE estado IS NULL").fetchone()[0]
    ultima = con.execute("""
        SELECT count(*) FROM ingest_manifest
        WHERE run_id = (SELECT max(run_id) FROM ingest_manifest) AND estado IS NOT NULL
    """).fetchone()[0]
    assert ultima == 17, "la última corrida debe registrar el veredicto de cada fuente"


def test_invariante_de_clasificacion(con):
    """RF-02: nada se descarta en silencio."""
    malas = con.execute("""
        SELECT source_file, rows_read, rows_loaded, rows_quarantined
        FROM ingest_manifest
        WHERE run_id = (SELECT max(run_id) FROM ingest_manifest)
          AND rows_read <> rows_loaded + rows_quarantined
    """).fetchall()
    assert malas == [], f"leídas != cargadas + cuarentena en: {malas}"


# --------------------------------------------------------------- P-02

def test_ninguna_observacion_disponible_antes_de_ocurrir(con):
    assert one(con, "SELECT count(*) FROM observations WHERE available_time < event_time") == 0


def test_el_check_temporal_esta_activo_en_la_base(tmp_path):
    """P-02 es estructural: la base debe rechazar la fila, no confiar en el código."""
    c = ingest.connect(tmp_path / "probe.duckdb")
    try:
        with pytest.raises(duckdb.ConstraintException):
            c.execute("""
                INSERT INTO observations VALUES
                ('x.csv','R1','PAT-0001',NULL,NULL,'HR','VITAL',
                 TIMESTAMP '2026-07-10 10:00:00', TIMESTAMP '2026-07-10 09:00:00',
                 70.0,NULL,70.0,'bpm','bpm',NULL,NULL,TRUE,FALSE,NULL,NULL,TRUE)
            """)
    finally:
        c.close()


def test_evidencia_huerfana_imposible(tmp_path):
    """RS-04: la clave foránea impide evidencia sin señal."""
    c = ingest.connect(tmp_path / "probe2.duckdb")
    try:
        with pytest.raises(duckdb.ConstraintException):
            c.execute("""
                INSERT INTO evidence VALUES
                ('NO-EXISTE','03_monitoring/vital_signs.csv','OBS-1','HR',
                 TIMESTAMP '2026-07-10 09:00:00', TIMESTAMP '2026-07-10 09:00:00',
                 'PRIMARY', 1.0)
            """)
    finally:
        c.close()


def test_latencias_por_dominio_coinciden_con_lo_medido(con):
    """RD-02: las reglas oficiales de disponibilidad producen estas latencias."""
    lag = dict(con.execute("""
        SELECT domain, round(median(epoch(available_time - event_time))/60, 1)
        FROM observations GROUP BY domain
    """).fetchall())
    assert lag["VITAL"] == 0.0
    assert lag["DEVICE"] == 0.0
    assert lag["WEARABLE"] == 4.0          # sync_datetime, mediana medida
    assert lag["LAB"] == 133.0             # result_datetime, mediana medida

    mx = dict(con.execute("""
        SELECT domain, round(max(epoch(available_time - event_time))/60, 1)
        FROM observations GROUP BY domain
    """).fetchall())
    assert mx["WEARABLE"] == 30.0
    assert mx["LAB"] == 360.0


# --------------------------------------------------------------- RD-03

def test_todo_registro_conserva_su_identificador_de_origen(con):
    for t in ("observations", "intervals", "clinical_facts"):
        assert one(con, f"""
            SELECT count(*) FROM {t}
            WHERE record_id IS NULL OR trim(record_id) = ''
               OR source_file IS NULL OR trim(source_file) = ''
        """) == 0, f"{t} tiene filas sin procedencia"


# --------------------------------------------------------------- RD-04

def test_temperatura_normalizada_a_grados_celsius(con):
    """166 filas en degF. Sin convertir, el máximo de TEMP sería 99.45."""
    assert one(con, "SELECT count(*) FROM observations WHERE variable_code='TEMP' "
                    "AND unit_raw='degF'") == 166
    assert one(con, "SELECT count(*) FROM observations WHERE variable_code='TEMP' "
                    "AND unit_canonical <> 'degC'") == 0
    mx = one(con, "SELECT max(value_num) FROM observations WHERE variable_code='TEMP'")
    assert mx < 45.0, f"TEMP canónica llega a {mx}: la conversión no se aplicó"

    # una fila concreta: 98.147 degF -> 36.75 degC
    v = one(con, "SELECT value_num FROM observations WHERE variable_code='TEMP' "
                 "AND unit_raw='degF' AND value_raw = 98.147 LIMIT 1")
    assert abs(v - 36.7483) < 1e-3, v


# --------------------------------------------------------------- RD-05

def test_retransmisiones_marcadas_y_no_descartadas(con):
    assert one(con, "SELECT count(*) FROM observations "
                    "WHERE source_system='MONITOR_RETRANSMIT'") == 540
    assert one(con, "SELECT count(DISTINCT patient_id) FROM observations "
                    "WHERE source_system='MONITOR_RETRANSMIT'") == 45
    # se conservan en la tabla: siguen siendo citables como evidencia QUALITY
    assert one(con, "SELECT count(*) FROM observations "
                    "WHERE source_system='MONITOR_RETRANSMIT' AND NOT is_duplicate") == 0
    assert one(con, "SELECT count(*) FROM observations "
                    "WHERE source_system='MONITOR_GATEWAY' AND is_duplicate") == 0


def test_toda_retransmision_cae_en_una_ventana_de_conectividad(con):
    """RD-07: la unión válida es por paciente, no por device_id."""
    assert one(con, """
        SELECT count(*) FROM observations o
        WHERE o.source_system='MONITOR_RETRANSMIT'
          AND NOT EXISTS (SELECT 1 FROM intervals c
                          WHERE c.kind='CONNECTIVITY' AND c.patient_id = o.patient_id
                            AND o.event_time BETWEEN c.start_time AND c.end_time)
    """) == 0


def test_una_retransmision_sin_ventana_no_pasa_con_su_timestamp(con):
    """Si no se puede acotar cuándo estuvo disponible, no se afirma que sí se pudo.

    En Candidate 1 las 540 están cubiertas, así que la regla no llega a
    ejercitarse sobre los datos reales. La invariante que sí se comprueba es la
    que importa: ninguna fila queda con disponibilidad afirmada sin respaldo.
    """
    sin_respaldo = one(con, """
        SELECT count(*) FROM observations o
        WHERE o.source_system = 'MONITOR_RETRANSMIT'
          AND coalesce(o.is_availability_known, TRUE)
          AND NOT EXISTS (SELECT 1 FROM intervals c
                          WHERE c.kind='CONNECTIVITY' AND c.patient_id = o.patient_id
                            AND o.event_time BETWEEN c.start_time AND c.end_time)
    """)
    assert sin_respaldo == 0, (
        f"{sin_respaldo} retransmisión(es) conservan available_time = timestamp sin una "
        f"ventana de conectividad que lo justifique")


def test_el_mecanismo_de_disponibilidad_incierta_funciona(tmp_path):
    """Candidate 1 no lo ejercita, así que se comprueba sobre datos construidos.

    Dos retransmisiones idénticas salvo por una cosa: una cae dentro de un corte
    de conectividad y la otra no. La primera debe quedar acotada al cierre del
    corte; la segunda, marcada, porque afirmar que estuvo disponible en su
    timestamp sería afirmar algo que sabemos falso.
    """
    import datetime as _dt

    c = ingest.connect(tmp_path / "sintetico.duckdb")
    try:
        base = _dt.datetime(2026, 7, 10, 12, 0, 0)
        c.execute("""
            INSERT INTO intervals VALUES
            ('04_context/connectivity_events.csv','CONN-X','PAT-0001','WRB-1','CONNECTIVITY',
             'DISCONNECTED','DISCONNECTED', TIMESTAMP '2026-07-10 11:00:00',
             TIMESTAMP '2026-07-10 14:00:00', TIMESTAMP '2026-07-10 11:00:00', NULL, NULL)
        """)
        # CUBIERTA cae dentro del corte 11:00–14:00; HUERFANA a las 18:00, fuera.
        for rid, t in (("CUBIERTA", base), ("HUERFANA", base + _dt.timedelta(hours=6))):
            c.execute(
                "INSERT INTO observations VALUES ('03_monitoring/vital_signs.csv',?,'PAT-0001',"
                "NULL,NULL,'HR','VITAL',?,?,70.0,NULL,70.0,'bpm','bpm',"
                "'MONITOR_RETRANSMIT','RETRANSMITTED',TRUE,FALSE,NULL,NULL,TRUE)",
                [rid, t, t])

        ajustadas, sin_ventana = ingest.resolver_disponibilidad_retransmitidas(c)
        assert ajustadas == 1, "la cubierta debía acotarse al cierre del corte"
        assert sin_ventana == 1, "la huérfana debía marcarse"

        cub = c.execute("SELECT available_time, is_availability_known FROM observations "
                        "WHERE record_id = 'CUBIERTA'").fetchone()
        assert cub[0] == _dt.datetime(2026, 7, 10, 14, 0, 0), "acotada al cierre"
        assert cub[1] is True

        hue = c.execute("SELECT available_time, is_availability_known FROM observations "
                        "WHERE record_id = 'HUERFANA'").fetchone()
        assert hue[1] is False, "sin ventana que la acote, no se afirma disponibilidad"

        snap = AsOfStore(c).snapshot("PAT-0001", base + _dt.timedelta(hours=12))
        assert "HUERFANA" not in {r for s in snap.series.values() for r in s.record_ids}
        assert {x.record_id: x.reason for x in snap.excluded}.get("HUERFANA") == \
            "AVAILABILITY_UNKNOWN"
        assert "CUBIERTA" in {r for s in snap.series.values() for r in s.record_ids}
    finally:
        c.close()


def test_las_filas_con_disponibilidad_incierta_no_llegan_al_motor(con):
    """No entran a las series, pero siguen en la tabla y son citables."""
    import datetime as _dt

    from hs.timeline import AsOfStore

    fila = con.execute("""
        SELECT patient_id, record_id, event_time FROM observations
        WHERE NOT coalesce(is_availability_known, TRUE) LIMIT 1""").fetchone()
    if not fila:
        pytest.skip("Candidate 1 no tiene filas con disponibilidad incierta")
    pid, rid, et = fila
    snap = AsOfStore(con).snapshot(pid, et + _dt.timedelta(hours=12))
    assert rid not in {r for s in snap.series.values() for r in s.record_ids}
    apartadas = {x.record_id: x.reason for x in snap.excluded}
    assert apartadas.get(rid) == "AVAILABILITY_UNKNOWN"


def test_sin_llaves_duplicadas_entre_filas_utilizables(con):
    assert one(con, """
        SELECT count(*) FROM (
            SELECT 1 FROM observations WHERE NOT is_duplicate
            GROUP BY patient_id, variable_code, event_time HAVING count(*) > 1)
    """) == 0


# --------------------------------------------------------------- RD-06

def test_gate_de_plausibilidad_propio_y_no_quality_flag(con):
    """549 valores imposibles vienen marcados OK en el origen."""
    total = one(con, "SELECT count(*) FROM observations WHERE NOT is_plausible")
    assert total == 762

    por_var = dict(con.execute(
        "SELECT variable_code, count(*) FROM observations WHERE NOT is_plausible GROUP BY 1"
    ).fetchall())
    assert por_var == {"SpO2": 734, "RR": 22, "SBP": 4, "HR": 1, "DBP": 1}

    ok = one(con, "SELECT count(*) FROM observations "
                  "WHERE NOT is_plausible AND quality_flag='OK'")
    assert ok == 549, "el gate propio debe cazar lo que quality_flag deja pasar"

    # y en sentido contrario: CHECK marca mayormente valores plausibles
    check_ok = one(con, "SELECT count(*) FROM observations "
                        "WHERE quality_flag='CHECK' AND is_plausible")
    assert check_ok > 3900, "descartar por quality_flag tiraría datos buenos"


def test_categoricos_no_se_marcan_implausibles(con):
    assert one(con, "SELECT count(*) FROM observations "
                    "WHERE variable_code='ACTIVITY_LEVEL' AND NOT is_plausible") == 0
    assert one(con, "SELECT count(*) FROM observations "
                    "WHERE variable_code='ACTIVITY_LEVEL' AND value_text IS NULL") == 0


# --------------------------------------------------------------- RD-01 / RD-09

def test_ventana_de_estudio(con):
    lo, hi = con.execute("SELECT min(event_time), max(event_time) FROM observations").fetchone()
    assert lo == STUDY_START and hi == STUDY_END


def test_un_encuentro_por_paciente(con):
    assert one(con, "SELECT count(*) FROM intervals WHERE kind='ENCOUNTER'") == 1000
    assert one(con, """
        SELECT count(*) FROM (
            SELECT patient_id FROM intervals WHERE kind='ENCOUNTER'
            GROUP BY 1 HAVING count(*) <> 1)
    """) == 0


def test_los_vitales_caen_dentro_del_encuentro(con):
    """RD-09: fuera del encuentro no hay dato ausente, no hay monitoreo."""
    assert one(con, """
        SELECT count(*) FROM observations o
        JOIN intervals e ON e.kind='ENCOUNTER' AND e.patient_id = o.patient_id
        WHERE o.domain='VITAL' AND o.event_time NOT BETWEEN e.start_time AND e.end_time
    """) == 0


def test_antecedentes_disponibles_antes_de_la_ventana(con):
    """Los antecedentes no introducen riesgo de leakage: ya estaban registrados."""
    assert one(con, "SELECT count(*) FROM clinical_facts "
                    "WHERE available_time >= TIMESTAMP '2026-07-01 00:00:00'") == 0


# --------------------------------------------------------------- volumen

def test_materia_prima_utilizable(con):
    usable = dict(con.execute("""
        SELECT variable_code, count(*) FROM observations
        WHERE is_plausible AND NOT is_duplicate AND domain='VITAL'
        GROUP BY 1
    """).fetchall())
    assert usable == {"HR": 441_877, "RR": 441_746, "SpO2": 441_264,
                      "TEMP": 147_949, "DBP": 74_418, "SBP": 74_413}
