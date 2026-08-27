"""API de consulta y decisión en vivo (RF-14, RF-15).

El endpoint que importa es `/decide`: el evaluador elige cualquier paciente y
cualquier instante, y el motor computa en el momento con la garantía as-of.
Nada precargado, nada preparado de antemano.

Seguridad (RNF-06): sólo lectura, sin SQL arbitrario, y el acceso a las filas
originales está restringido por lista blanca a las 17 fuentes declaradas —
`source_file` viene de la salida del propio sistema, pero igual se valida.
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
from contextlib import asynccontextmanager
from typing import Any

import duckdb
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .. import paths
from ..detect import EventConfig, signal_id
from ..domain.scoring import Assessment, ScoringConfig, assess
from ..timeline import AsOfStore

_lock = threading.Lock()
_estado: dict[str, Any] = {}


@asynccontextmanager
async def _ciclo(_app: FastAPI):
    d = yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8"))
    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    cfg = ScoringConfig.from_dict(d)
    _estado.update(
        con=con, cfg=cfg, ev=EventConfig.from_dict(d),
        store=AsOfStore(con, lookback=cfg.evidencia + cfg.baseline),
        fuentes={r[0] for r in con.execute(
            "SELECT DISTINCT source_file FROM observations "
            "UNION SELECT DISTINCT source_file FROM intervals "
            "UNION SELECT DISTINCT source_file FROM clinical_facts").fetchall()},
    )
    try:
        yield
    finally:
        con.close()
        _estado.clear()


app = FastAPI(
    title="HealthSignal LATAM",
    description="Motor de concordancia sobre RISA Data V1.0. Apoyo a la decisión: "
                "no emite diagnósticos ni prescripciones.",
    version="0.2.0",
    lifespan=_ciclo,
)


def _q(sql: str, params: list | None = None) -> list[tuple]:
    with _lock:
        return _estado["con"].execute(sql, params or []).fetchall()


def _dicts(sql: str, params: list | None = None) -> list[dict]:
    with _lock:
        cur = _estado["con"].execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- estado

@app.get("/health", tags=["sistema"])
def health() -> dict:
    n = _q("SELECT count(*) FROM signals")[0][0]
    obs = _q("SELECT count(*) FROM observations")[0][0]
    return {"ok": True, "señales": n, "observaciones": obs,
            "model_version": _estado["cfg"].model_version}


# --------------------------------------------------------------------------- señales

@app.get("/signals", tags=["señales"])
def listar_señales(
    priority: str | None = Query(None, description="LOW | MEDIUM | HIGH | CRITICAL"),
    patient: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """Ranking de señales. El orden es el argumento: por qué A antes que B (RF-13)."""
    cond, params = [], []
    if priority:
        cond.append("priority_level = ?")
        params.append(priority.upper())
    if patient:
        cond.append("patient_id = ?")
        params.append(patient)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    params.append(limit)
    return _dicts(f"""
        SELECT signal_id, patient_id, decision_datetime, risk_score, confidence_score,
               priority_level, k_concordantes, suppressions, evidence_start, evidence_end,
               explanation, model_version
        FROM signals {where}
        ORDER BY risk_score DESC, signal_id LIMIT ?
    """, params)


@app.get("/signals/{sid}", tags=["señales"])
def ver_señal(sid: str) -> dict:
    """Una señal con toda su evidencia, agrupada por rol."""
    s = _dicts("SELECT * FROM signals WHERE signal_id = ?", [sid])
    if not s:
        raise HTTPException(404, f"señal desconocida: {sid}")
    ev = _dicts("""
        SELECT source_file, record_id, variable_code, event_datetime,
               available_datetime, evidence_role, contribution
        FROM evidence WHERE signal_id = ?
        ORDER BY evidence_role, event_datetime
    """, [sid])
    por_rol: dict[str, list] = {}
    for e in ev:
        por_rol.setdefault(e["evidence_role"], []).append(e)
    return {"señal": s[0], "evidencia": por_rol, "total_evidencia": len(ev)}


# --------------------------------------------------------------------------- pacientes

@app.get("/patients", tags=["pacientes"])
def listar_pacientes(limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
    return _dicts("""
        SELECT p.patient_id, p.age_years, p.sex_at_birth, p.care_program, p.region_type,
               p.baseline_risk_profile,
               (SELECT count(*) FROM signals s WHERE s.patient_id = p.patient_id) AS señales,
               (SELECT max(risk_score) FROM signals s WHERE s.patient_id = p.patient_id) AS riesgo_max
        FROM patients p ORDER BY riesgo_max DESC NULLS LAST, p.patient_id LIMIT ?
    """, [limit])


@app.get("/patients/{pid}/timeline", tags=["pacientes"])
def timeline(
    pid: str,
    desde: dt.datetime | None = None,
    hasta: dt.datetime | None = None,
    as_of: dt.datetime | None = Query(None, description="oculta lo no disponible en ese instante"),
    variables: str = "HR,RR,SpO2,TEMP",
) -> dict:
    """Series para graficar. Con `as_of` devuelve sólo lo conocido en ese instante."""
    codes = [v.strip() for v in variables.split(",") if v.strip()]
    cond = ["patient_id = ?", f"variable_code IN ({','.join('?' * len(codes))})",
            "NOT is_duplicate"]
    params: list = [pid, *codes]
    if desde:
        cond.append("event_time >= ?")
        params.append(desde)
    if hasta:
        cond.append("event_time <= ?")
        params.append(hasta)
    if as_of:
        cond.append("available_time <= ?")
        params.append(as_of)

    filas = _dicts(f"""
        SELECT variable_code, event_time, available_time, value_num, value_text,
               unit_canonical, record_id, is_plausible, quality_flag
        FROM observations WHERE {' AND '.join(cond)} ORDER BY variable_code, event_time
    """, params)
    series: dict[str, list] = {}
    for f in filas:
        series.setdefault(f["variable_code"], []).append(f)

    ctx = _dicts("""
        SELECT kind, subtype, value_text, start_time, end_time, record_id, source_file
        FROM intervals WHERE patient_id = ? ORDER BY start_time
    """, [pid])
    return {"patient_id": pid, "as_of": as_of, "series": series, "intervalos": ctx}


# --------------------------------------------------------------------------- decisión

def _serializar(a: Assessment) -> dict:
    return {
        "signal_id": signal_id(a),
        "patient_id": a.patient_id,
        "decision_datetime": a.T,
        "evidence_start": a.evidence_start,
        "evidence_end": a.evidence_end,
        "risk_score": round(a.risk, 4),
        "confidence_score": round(a.confidence, 4),
        "priority_level": a.priority,
        "k_concordantes": a.k,
        "explanation": a.explicacion,
        "model_version": a.model_version,
        "canales": [
            {"variable_code": c.variable_code, "contribucion": round(c.s, 3),
             "nivel_sigmas": round(c.nivel, 2), "deriva_sigmas": round(c.pendiente, 2),
             "persistencia": round(c.persistencia, 2), "cobertura": round(c.cobertura, 3),
             "baseline_mediana": round(c.mediana_baseline, 2),
             "baseline_escala": round(c.escala, 3), "ultimo_valor": round(c.ultimo_valor, 2),
             "n_evidencia": c.n_evidencia, "n_baseline": c.n_baseline}
            for c in sorted(a.canales.values(), key=lambda x: -x.s)
        ],
        "reglas": [
            {"regla": s.regla, "fuerza": s.fuerza, "motivo": s.motivo,
             "citas": [c.record_id for c in s.citas]}
            for s in a.supresiones
        ],
        "evidencia": [
            {"source_file": c.source_file, "record_id": c.record_id,
             "variable_code": c.variable_code, "event_datetime": c.event_time,
             "available_datetime": c.available_time, "evidence_role": c.role,
             "contribution": c.contribution}
            for c in a.citas
        ],
    }


@app.get("/decide", tags=["decisión"])
def decidir(
    patient: str = Query(..., description="p.ej. PAT-0869"),
    at: dt.datetime = Query(..., description="instante de decisión, p.ej. 2026-07-20T18:00:00"),
) -> dict:
    """Computa una decisión en vivo, en el instante que se pida.

    Usa exclusivamente evidencia con `available_time <= at`. Nada precargado:
    el evaluador puede elegir un paciente y una hora que nadie preparó (RF-14).
    """
    store: AsOfStore = _estado["store"]
    cfg: ScoringConfig = _estado["cfg"]
    if not _q("SELECT 1 FROM patients WHERE patient_id = ?", [patient]):
        raise HTTPException(404, f"paciente desconocido: {patient}")

    with _lock:
        snap = store.snapshot(patient, at)
    a = assess(snap, cfg)
    r = _serializar(a)
    r["dentro_del_encuentro"] = snap.within_encounter()
    r["observaciones_visibles"] = snap.n_observations()
    r["apartadas"] = [{"record_id": x.record_id, "motivo": x.reason,
                       "variable_code": x.variable_code} for x in snap.excluded]
    return r


# --------------------------------------------------------------------------- trazabilidad

@app.get("/source-row", tags=["trazabilidad"])
def fila_original(source_file: str, record_id: str) -> dict:
    """La fila tal cual está en el CSV original de RISA (RF-15).

    `source_file` se valida contra la lista de fuentes cargadas: la ruta nunca
    se construye con texto libre del pedido.
    """
    if source_file not in _estado["fuentes"]:
        raise HTTPException(400, f"fuente no reconocida: {source_file}")
    ruta = paths.raw_path(source_file)
    if not ruta.exists():
        raise HTTPException(404, "el archivo original no está disponible en este entorno")

    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        clave = lector.fieldnames[0]          # la PK es siempre la primera columna
        for fila in lector:
            if fila.get(clave) == record_id:
                return {"source_file": source_file, "record_id": record_id,
                        "clave": clave, "fila": fila}
    raise HTTPException(404, f"{record_id} no existe en {source_file}")


# --------------------------------------------------------------------------- interfaz

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def raiz() -> str:
    ui = paths.ROOT / "ui" / "index.html"
    if ui.exists():
        return ui.read_text(encoding="utf-8")
    return ("<h1>HealthSignal LATAM</h1>"
            "<p>API activa. Documentación interactiva en <a href='/docs'>/docs</a>.</p>")
