"""RF-14, RF-15 — decisión en vivo y trazabilidad hasta el archivo original.

El criterio que estas pruebas fijan es el de la rúbrica: el evaluador nombra un
paciente y una hora que nadie preparó, y el sistema responde con la garantía
temporal intacta.
"""
from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from hs import paths

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    if not paths.WAREHOUSE.exists():
        pytest.skip("Falta el almacén: correr scripts/00_ingest.py")
    from hs.api import app
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_decide_en_un_instante_arbitrario(client):
    """RF-14: nada precargado. El instante lo elige quien pregunta."""
    r = client.get("/decide", params={"patient": "PAT-0869",
                                      "at": "2026-07-20T18:00:00"})
    assert r.status_code == 200
    d = r.json()
    assert d["patient_id"] == "PAT-0869"
    assert 0.0 <= d["risk_score"] <= 1.0
    assert d["priority_level"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert d["canales"] and d["evidencia"]
    assert d["explanation"]


def test_decide_puede_incluir_lectura_para_revision(client, monkeypatch):
    """La UI pide la narrativa explicitamente; el endpoint base sigue determinista."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.get("/decide", params={"patient": "PAT-0869",
                                      "at": "2026-07-20T18:00:00",
                                      "narrative": "true"})
    assert r.status_code == 200
    n = r.json()["narrative"]
    assert n["source"] == "deterministic_fallback"
    assert n["summary"] and len(n["review_points"]) >= 2
    assert n["findings"]


def test_flag_de_ia_desde_la_api_usa_fallback_y_no_expone_clave(client):
    anterior = client.get("/narrative/settings").json()
    try:
        r = client.put("/narrative/settings", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert "api_key" not in r.text.lower()

        d = client.get("/decide", params={"patient": "PAT-0869",
                                           "at": "2026-07-20T18:00:00",
                                           "narrative": "true"}).json()
        assert d["narrative"]["source"] == "deterministic_fallback"
    finally:
        client.put("/narrative/settings", json={"enabled": anterior["enabled"]})


def test_decide_respeta_la_causalidad(client):
    """Toda la evidencia devuelta estaba disponible en el instante pedido."""
    at = dt.datetime(2026, 7, 20, 18, 0, 0)
    r = client.get("/decide", params={"patient": "PAT-0869", "at": at.isoformat()})
    for e in r.json()["evidencia"]:
        assert dt.datetime.fromisoformat(e["available_datetime"]) <= at, e


def test_decide_en_dos_instantes_no_devuelve_lo_mismo(client):
    """La decisión depende de T: es una decisión, no una consulta a una tabla."""
    a = client.get("/decide", params={"patient": "PAT-0869",
                                      "at": "2026-07-20T12:00:00"}).json()
    b = client.get("/decide", params={"patient": "PAT-0869",
                                      "at": "2026-07-20T18:00:00"}).json()
    assert a["risk_score"] != b["risk_score"] or a["k_concordantes"] != b["k_concordantes"]


def test_decide_es_reproducible(client):
    """P-05: misma pregunta, misma respuesta."""
    p = {"patient": "PAT-0869", "at": "2026-07-20T18:00:00"}
    a = client.get("/decide", params=p).json()
    b = client.get("/decide", params=p).json()
    assert a["risk_score"] == b["risk_score"]
    assert a["explanation"] == b["explanation"]


def test_decide_rechaza_paciente_inexistente(client):
    assert client.get("/decide", params={"patient": "PAT-9999",
                                         "at": "2026-07-20T18:00:00"}).status_code == 404


def test_historia_hasta_un_corte_recorre_el_periodo_y_conserva_causalidad(client):
    hasta = dt.datetime(2026, 7, 13, 12, 20)
    r = client.get(
        "/patients/PAT-0002/history-analysis",
        params={"hasta": hasta.isoformat()},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["history_mode"] == "until"
    assert dt.datetime.fromisoformat(d["history_end"]) == hasta
    assert dt.datetime.fromisoformat(d["decision_datetime"]) == hasta
    assert d["evaluations_count"] == sum(d["priority_counts"].values())
    assert all(dt.datetime.fromisoformat(p["at"]) <= hasta for p in d["risk_trajectory"])
    assert dt.datetime.fromisoformat(d["historical_peak"]["decision_datetime"]) <= hasta
    for assessment in (d, d["historical_peak"]):
        for evidence in assessment["evidencia"]:
            assert dt.datetime.fromisoformat(evidence["available_datetime"]) <= dt.datetime.fromisoformat(
                assessment["decision_datetime"]
            )


def test_historia_completa_distingue_maximo_historico_del_estado_final(client):
    """Un CRITICAL pasado no convierte el cierre recuperado en CRITICAL."""
    r = client.get("/patients/PAT-0869/history-analysis")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["history_mode"] == "complete"
    assert d["historical_peak"]["priority_level"] == "CRITICAL"
    assert d["priority_level"] == "LOW"
    assert d["historical_peak"]["risk_score"] > d["risk_score"]


def test_historia_rechaza_corte_fuera_del_encuentro(client):
    r = client.get(
        "/patients/PAT-0002/history-analysis",
        params={"hasta": "2026-07-21T12:20:00"},
    )
    assert r.status_code == 422


def test_señales_y_evidencia_por_rol(client):
    r = client.get("/signals", params={"priority": "CRITICAL", "limit": 5})
    assert r.status_code == 200
    filas = r.json()
    if not filas:
        pytest.skip("Sin señales CRITICAL: correr scripts/02_detect.py")
    d = client.get(f"/signals/{filas[0]['signal_id']}").json()
    assert d["total_evidencia"] > 0
    assert "PRIMARY" in d["evidencia"]


def test_timeline_as_of_oculta_lo_no_disponible(client):
    at = "2026-07-20T18:00:00"
    todo = client.get("/patients/PAT-0869/timeline",
                      params={"hasta": at}).json()
    visible = client.get("/patients/PAT-0869/timeline",
                         params={"hasta": at, "as_of": at}).json()
    n_todo = sum(len(v) for v in todo["series"].values())
    n_vis = sum(len(v) for v in visible["series"].values())
    assert n_vis <= n_todo


def test_trazabilidad_hasta_el_csv_original(client):
    if not paths.RAW.exists():
        pytest.skip("Faltan los CSV originales")
    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    try:
        fila = con.execute("""
            SELECT source_file, record_id FROM evidence
            WHERE source_file LIKE '%vital_signs%' LIMIT 1""").fetchone()
    finally:
        con.close()
    if not fila:
        pytest.skip("Sin evidencia exportada")
    r = client.get("/source-row", params={"source_file": fila[0], "record_id": fila[1]})
    assert r.status_code == 200, r.text
    assert r.json()["fila"]["observation_id"] == fila[1]


def test_la_ruta_no_se_construye_con_texto_libre(client):
    """RNF-06: source_file va contra lista blanca, no al sistema de archivos."""
    for malo in ("../../../etc/passwd", "C:/Windows/win.ini",
                 "03_monitoring/../../secretos.csv"):
        r = client.get("/source-row", params={"source_file": malo, "record_id": "x"})
        assert r.status_code == 400, f"{malo} no fue rechazado"
