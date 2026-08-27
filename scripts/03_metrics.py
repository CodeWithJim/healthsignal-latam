"""Etapa 7: métricas sin Gold Standard.

La organización guarda las etiquetas, así que el conjunto de negativos se
construye desde los marcadores públicos que el propio escenario declara
(RF-08). Eso permite reportar una tasa de falsas alertas defendible.

    .venv\\Scripts\\python.exe scripts\\03_metrics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402

from hs import paths  # noqa: E402


def rule(t: str) -> None:
    print("\n" + "=" * 82)
    print(t)
    print("=" * 82)


# Marcadores públicos de los patrones que el escenario declara como distractores.
DISTRACTORES = {
    "CONTEXTUAL_actividad": """
        EXISTS (SELECT 1 FROM intervals i
                WHERE i.patient_id = s.patient_id AND i.kind = 'CONTEXT'
                  AND i.subtype = 'PHYSICAL_ACTIVITY' AND i.value_text IN ('HIGH','MODERATE')
                  AND i.start_time <= s.evidence_end AND i.end_time >= s.evidence_start)""",
    "CONTEXTUAL_recuperacion": """
        EXISTS (SELECT 1 FROM intervals i
                WHERE i.patient_id = s.patient_id AND i.kind = 'CONTEXT'
                  AND i.subtype = 'RECOVERY_PHASE'
                  AND i.start_time <= s.evidence_end AND i.end_time >= s.evidence_start)""",
    "DATA_QUALITY_low_signal": """
        EXISTS (SELECT 1 FROM observations o
                WHERE o.patient_id = s.patient_id AND o.quality_flag = 'LOW_SIGNAL'
                  AND o.event_time BETWEEN s.evidence_start AND s.evidence_end)""",
    "DATA_QUALITY_implausible": """
        EXISTS (SELECT 1 FROM observations o
                WHERE o.patient_id = s.patient_id AND NOT o.is_plausible
                  AND o.event_time BETWEEN s.evidence_start AND s.evidence_end)""",
    "DATA_QUALITY_retransmision": """
        EXISTS (SELECT 1 FROM observations o
                WHERE o.patient_id = s.patient_id AND o.is_duplicate
                  AND o.event_time BETWEEN s.evidence_start AND s.evidence_end)""",
    "CONECTIVIDAD": """
        EXISTS (SELECT 1 FROM intervals i
                WHERE i.patient_id = s.patient_id AND i.kind = 'CONNECTIVITY'
                  AND i.start_time <= s.evidence_end AND i.end_time >= s.evidence_start)""",
}


def main() -> int:
    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    total = con.execute("SELECT count(*) FROM signals").fetchone()[0]
    if total == 0:
        print("No hay señales: correr scripts/02_detect.py")
        return 1
    altas = con.execute(
        "SELECT count(*) FROM signals WHERE priority_level IN ('HIGH','CRITICAL')").fetchone()[0]

    rule(f"CONJUNTO DE SEÑALES  ·  {total:,} en total  ·  {altas:,} de prioridad HIGH o CRITICAL")
    for p, n, r, c in con.execute("""
        SELECT priority_level, count(*), round(avg(risk_score), 3), round(avg(confidence_score), 3)
        FROM signals GROUP BY 1
        ORDER BY CASE priority_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                                     WHEN 'MEDIUM' THEN 2 ELSE 3 END
    """).fetchall():
        print(f"  {p:9} {n:5,}   riesgo medio {r:.3f}   confianza media {c:.3f}")

    rule("CE-04 · IMPACTO EN DISTRACTORES")
    print("  Señales HIGH+ cuya ventana de evidencia coincide con un marcador conocido.")
    print("  Coincidir no es fallar: fallar es coincidir SIN registrar la supresión.\n")
    print(f"  {'marcador':30} {'coincide':>9} {'no registrado':>14}")
    print("  " + "-" * 56)
    peor = 0
    for nombre, pred in DISTRACTORES.items():
        n, sin = con.execute(f"""
            SELECT count(*), count(*) FILTER (WHERE coalesce(s.suppressions, '') = '')
            FROM signals s
            WHERE s.priority_level IN ('HIGH','CRITICAL') AND {pred}
        """).fetchone()
        peor += sin
        print(f"  {nombre:30} {n:9,} {sin:14,}")
    print(f"\n  Tasa de impacto en distractores: {peor}/{altas} = "
          f"{100 * peor / max(1, altas):.1f}%   (objetivo: cerca de cero)")
    print("  Se cuenta como impacto la señal que coincide con un marcador y no deja")
    print("  constancia de haberlo evaluado, esté suprimida o no.")

    rule("REGLAS ACTIVADAS SOBRE LAS SEÑALES EMITIDAS")
    filas = con.execute("""
        SELECT regla, count(*), count(*) FILTER (WHERE priority_level IN ('HIGH','CRITICAL'))
        FROM (SELECT unnest(string_split(suppressions, ',')) AS regla, priority_level
              FROM signals WHERE coalesce(suppressions, '') <> '')
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    if filas:
        print(f"  {'regla':24} {'señales':>8} {'de las cuales HIGH+':>21}")
        for r, n, alt in filas:
            print(f"  {r:24} {n:8,} {alt:21,}")
    else:
        print("  Ninguna regla se activó sobre las señales emitidas.")

    rule("CE-07 · INTEGRACIÓN MULTIFUENTE")
    for n, fuentes in con.execute("""
        SELECT count(*), n FROM (
            SELECT signal_id, count(DISTINCT source_file) AS n
            FROM evidence GROUP BY 1) GROUP BY n ORDER BY n DESC
    """).fetchall():
        print(f"  {n:5,} señal(es) sustentadas en {fuentes} archivo(s) distinto(s) de RISA")
    ej = con.execute("""
        SELECT signal_id, count(DISTINCT source_file) AS n
        FROM evidence GROUP BY 1 ORDER BY n DESC, signal_id LIMIT 1
    """).fetchone()
    if ej:
        print(f"\n  Ejemplo con más fuentes: {ej[0]} ({ej[1]} archivos)")
        for r in con.execute(
                "SELECT source_file, evidence_role, count(*) FROM evidence "
                "WHERE signal_id = ? GROUP BY 1,2 ORDER BY 1", [ej[0]]).fetchall():
            print(f"    {r[0]:44} {r[1]:11} {r[2]:3}")

    rule("CE-05 · ANTICIPACIÓN")
    print("  Horas entre la decisión declarada y el momento de máxima desviación")
    print("  posterior del canal dominante, dentro de las 24 h siguientes.\n")
    filas = con.execute("""
        WITH alta AS (
          SELECT s.signal_id, s.patient_id, s.decision_datetime,
                 (SELECT e.variable_code FROM evidence e
                  WHERE e.signal_id = s.signal_id AND e.evidence_role = 'PRIMARY' LIMIT 1) AS var
          FROM signals s WHERE s.priority_level IN ('HIGH','CRITICAL')
        ), pico AS (
          SELECT a.signal_id, a.decision_datetime,
                 max(CASE WHEN a.var = 'SpO2' THEN -o.value_num ELSE o.value_num END) AS extremo,
                 arg_max(o.event_time,
                         CASE WHEN a.var = 'SpO2' THEN -o.value_num ELSE o.value_num END) AS t_pico
          FROM alta a JOIN observations o
            ON o.patient_id = a.patient_id AND o.variable_code = a.var
           AND o.is_plausible AND NOT o.is_duplicate
           AND o.event_time > a.decision_datetime
           AND o.event_time <= a.decision_datetime + INTERVAL 24 HOUR
          GROUP BY 1,2
        )
        SELECT round(quantile_cont(epoch(t_pico - decision_datetime)/3600, 0.25), 1),
               round(median(epoch(t_pico - decision_datetime)/3600), 1),
               round(quantile_cont(epoch(t_pico - decision_datetime)/3600, 0.75), 1),
               count(*)
        FROM pico
    """).fetchone()
    if filas and filas[3]:
        print(f"  p25 {filas[0]} h   mediana {filas[1]} h   p75 {filas[2]} h   "
              f"sobre {filas[3]} señales")
        print(f"  {'OK' if (filas[1] or 0) > 2 else 'por debajo del objetivo'} "
              f"(objetivo CE-05: mediana > 2 h)")

    rule("CE-01 / CE-02 · AUDITORÍA DE LA SALIDA")
    fugas = con.execute("""
        SELECT count(*) FROM evidence e JOIN signals s USING (signal_id)
        WHERE e.available_datetime > s.decision_datetime""").fetchone()[0]
    sin_ev = con.execute("""
        SELECT count(*) FROM signals s WHERE NOT EXISTS
        (SELECT 1 FROM evidence e WHERE e.signal_id = s.signal_id)""").fetchone()[0]
    print(f"  CE-01 evidencia posterior a la decisión: {fugas}   {'OK' if not fugas else 'FALLA'}")
    print(f"  CE-02 señales sin evidencia:             {sin_ev}   {'OK' if not sin_ev else 'FALLA'}")

    print()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
