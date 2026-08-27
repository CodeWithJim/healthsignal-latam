"""Etapa 6: dictámenes -> tablas -> CSV.

Las restricciones del esquema hacen el trabajo de validación: un puntaje fuera de
[0,1], una prioridad inventada, una ventana posterior a la decisión o una fila de
evidencia sin señal fallan al insertar. Exportar es después un COPY, y pasar
`validate_submission.py` deja de ser una tarea para ser una consecuencia
(RS-01..RS-06).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..detect.runner import signal_id
from ..domain.scoring import Assessment

COLUMNAS_SIGNALS = ("signal_id", "patient_id", "decision_datetime", "risk_score",
                    "priority_level", "confidence_score", "evidence_start",
                    "evidence_end", "explanation", "model_version",
                    "k_concordantes", "suppressions")
COLUMNAS_EVIDENCE = ("signal_id", "source_file", "record_id", "variable_code",
                     "event_datetime", "available_datetime", "evidence_role",
                     "contribution")


def _iso(t: dt.datetime) -> str:
    """Sin zona horaria, como los datos de origen.

    Mezclar fechas con y sin offset hace que el validador oficial termine con un
    TypeError no capturado en vez de un [FAIL] legible (RS-05).
    """
    return t.replace(tzinfo=None).isoformat(sep=" ")


def guardar(con, señales: list[Assessment], run_id: str) -> tuple[int, int]:
    """Inserta señales y evidencia en el almacén. Devuelve (n_señales, n_evidencia)."""
    con.execute("DELETE FROM evidence")
    con.execute("DELETE FROM signals")

    filas_s, filas_e = [], []
    for a in señales:
        sid = signal_id(a)
        filas_s.append((sid, a.patient_id, a.T, round(a.risk, 6),
                        round(a.confidence, 6), a.priority,
                        a.evidence_start, a.evidence_end, a.explicacion,
                        a.model_version, run_id, a.k,
                        ",".join(s.regla for s in a.supresiones) or None))
        for c in a.citas:
            filas_e.append((sid, c.source_file, c.record_id, c.variable_code,
                            c.event_time, c.available_time, c.role,
                            None if c.contribution is None else round(c.contribution, 6)))

    con.executemany(
        "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", filas_s)
    con.executemany(
        "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)", filas_e)
    return len(filas_s), len(filas_e)


def exportar(con, destino: Path) -> tuple[Path, Path]:
    """Vuelca las tablas a los dos CSV del contrato oficial."""
    destino.mkdir(parents=True, exist_ok=True)
    sp, ep = destino / "signals.csv", destino / "evidence.csv"

    con.execute(f"""
        COPY (SELECT {', '.join(COLUMNAS_SIGNALS)} FROM signals ORDER BY signal_id)
        TO '{str(sp).replace(chr(92), '/')}' (HEADER, DELIMITER ',')
    """)
    con.execute(f"""
        COPY (SELECT {', '.join(COLUMNAS_EVIDENCE)} FROM evidence
              ORDER BY signal_id, evidence_role, record_id)
        TO '{str(ep).replace(chr(92), '/')}' (HEADER, DELIMITER ',')
    """)
    return sp, ep


def auditar_causalidad(con) -> list[tuple]:
    """CE-01: ninguna fila de evidencia posterior a la decisión de su señal.

    No confía en el motor: lo comprueba sobre lo que efectivamente se exportó.
    """
    return con.execute("""
        SELECT e.signal_id, e.record_id, e.available_datetime, s.decision_datetime
        FROM evidence e JOIN signals s USING (signal_id)
        WHERE e.available_datetime > s.decision_datetime
    """).fetchall()


def auditar_huerfanas(con) -> tuple[int, int]:
    """CE-02: toda señal con evidencia, toda evidencia con señal."""
    sin_evidencia = con.execute("""
        SELECT count(*) FROM signals s
        WHERE NOT EXISTS (SELECT 1 FROM evidence e WHERE e.signal_id = s.signal_id)
    """).fetchone()[0]
    huerfanas = con.execute("""
        SELECT count(*) FROM evidence e
        WHERE NOT EXISTS (SELECT 1 FROM signals s WHERE s.signal_id = e.signal_id)
    """).fetchone()[0]
    return sin_evidencia, huerfanas
