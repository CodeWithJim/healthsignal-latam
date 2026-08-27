"""RS-01..RS-06 — el contrato de entrega, verificado con el script oficial.

No parafrasea el validador: lo ejecuta. Y comprueba aparte las cosas que el
validador no mira pero el jurado sí, como que cada record_id citado exista de
verdad en los archivos de RISA.
"""
from __future__ import annotations

import csv
import datetime as dt
import subprocess
import sys

import duckdb
import pytest

from hs import paths

SIGNALS = paths.RESULTS / "signals.csv"
EVIDENCE = paths.RESULTS / "evidence.csv"

REQUERIDAS_SIGNALS = {"signal_id", "patient_id", "decision_datetime", "risk_score",
                      "priority_level", "evidence_start", "evidence_end",
                      "explanation", "model_version"}
REQUERIDAS_EVIDENCE = {"signal_id", "source_file", "record_id", "event_datetime",
                       "available_datetime", "evidence_role"}
PRIORIDADES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
ROLES = {"PRIMARY", "SUPPORTING", "CONTEXT", "QUALITY"}


def _leer(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def signals():
    if not SIGNALS.exists():
        pytest.skip("Falta results/signals.csv: correr scripts/02_detect.py")
    return _leer(SIGNALS)


@pytest.fixture(scope="module")
def evidence():
    if not EVIDENCE.exists():
        pytest.skip("Falta results/evidence.csv: correr scripts/02_detect.py")
    return _leer(EVIDENCE)


# --------------------------------------------------------------- el script oficial

def test_el_validador_oficial_no_reporta_errores():
    """CE-03. Es una compuerta, no un criterio: vale cero puntos, pero sin ella
    la trazabilidad no se evalúa."""
    v = paths.validador()
    if v is None or not SIGNALS.exists():
        pytest.skip("Falta el validador oficial o results/")
    r = subprocess.run([sys.executable, str(v), str(paths.RESULTS)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (
        "validate_submission.py rechazó la entrega:\n" + r.stdout + r.stderr)
    assert "VALID SUBMISSION FORMAT" in r.stdout


def test_el_validador_reconoce_los_patient_id_contra_risa():
    v = paths.validador()
    if v is None or not SIGNALS.exists() or not paths.RAW.exists():
        pytest.skip("Falta el validador o los datos de RISA")
    r = subprocess.run([sys.executable, str(v), str(paths.RESULTS), "--risa", str(paths.RAW)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All patient_id values exist in supplied RISA data" in r.stdout


# --------------------------------------------------------------- estructura

def test_columnas_requeridas(signals, evidence):
    assert REQUERIDAS_SIGNALS <= set(signals[0])
    assert REQUERIDAS_EVIDENCE <= set(evidence[0])


def test_restricciones_de_contenido(signals):
    ids = set()
    for i, r in enumerate(signals, start=2):
        assert r["signal_id"] and r["signal_id"] not in ids, f"fila {i}: signal_id"
        ids.add(r["signal_id"])
        assert r["patient_id"]
        assert 0.0 <= float(r["risk_score"]) <= 1.0, f"fila {i}: risk_score"
        if r.get("confidence_score", "").strip():
            assert 0.0 <= float(r["confidence_score"]) <= 1.0
        assert r["priority_level"] in PRIORIDADES, f"fila {i}: {r['priority_level']}"
        assert r["explanation"].strip() and r["model_version"].strip()


def test_orden_temporal_por_señal(signals):
    for i, r in enumerate(signals, start=2):
        a = dt.datetime.fromisoformat(r["evidence_start"])
        b = dt.datetime.fromisoformat(r["evidence_end"])
        c = dt.datetime.fromisoformat(r["decision_datetime"])
        assert a <= b <= c, f"fila {i}: evidence_start <= evidence_end <= decision"


def test_fechas_sin_zona_horaria(signals, evidence):
    """RS-05: mezclar naive y aware hace crashear al validador con TypeError."""
    for filas, cols in ((signals, ("decision_datetime", "evidence_start", "evidence_end")),
                        (evidence, ("event_datetime", "available_datetime"))):
        for r in filas:
            for c in cols:
                assert dt.datetime.fromisoformat(r[c]).tzinfo is None, f"{c}={r[c]}"


# --------------------------------------------------------------- integridad relacional

def test_integridad_bidireccional(signals, evidence):
    """RS-04: el validador rechaza los dos casos."""
    ids = {r["signal_id"] for r in signals}
    con_evidencia = {r["signal_id"] for r in evidence}
    assert not (ids - con_evidencia), f"señales sin evidencia: {ids - con_evidencia}"
    assert not (con_evidencia - ids), f"evidencia huérfana: {con_evidencia - ids}"


def test_causalidad_de_toda_la_evidencia(signals, evidence):
    """CE-01: la propiedad que define si la anticipación es válida."""
    decision = {r["signal_id"]: dt.datetime.fromisoformat(r["decision_datetime"])
                for r in signals}
    for i, r in enumerate(evidence, start=2):
        assert r["evidence_role"] in ROLES, f"fila {i}: {r['evidence_role']}"
        av = dt.datetime.fromisoformat(r["available_datetime"])
        assert av <= decision[r["signal_id"]], (
            f"fila {i}: {r['record_id']} disponible en {av}, posterior a la decisión")


def test_todo_record_id_existe_en_risa(evidence):
    """RF-15: el validador no lo comprueba, el jurado auditando una señal sí."""
    if not paths.WAREHOUSE.exists():
        pytest.skip("Falta el almacén")
    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    try:
        faltantes = con.execute("""
            SELECT e.source_file, e.record_id FROM evidence e
            WHERE NOT EXISTS (SELECT 1 FROM observations o
                              WHERE o.source_file = e.source_file AND o.record_id = e.record_id)
              AND NOT EXISTS (SELECT 1 FROM intervals i
                              WHERE i.source_file = e.source_file AND i.record_id = e.record_id)
            LIMIT 5
        """).fetchall()
        assert not faltantes, f"record_id inexistentes: {faltantes}"
    finally:
        con.close()


def test_toda_señal_tiene_evidencia_primaria(evidence):
    por_señal: dict[str, set[str]] = {}
    for r in evidence:
        por_señal.setdefault(r["signal_id"], set()).add(r["evidence_role"])
    sin_primaria = [s for s, roles in por_señal.items() if "PRIMARY" not in roles]
    assert not sin_primaria, f"señales sin evidencia PRIMARY: {sin_primaria[:5]}"


def test_volumen_dentro_del_rango_previsto(signals):
    """CE-08: entre 10^2 y 10^3 señales. Más alertas no es mejor desempeño."""
    assert 50 <= len(signals) <= 3000, f"{len(signals)} señales"
