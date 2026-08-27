"""CE-06: tabla de ablación.

Cada mecanismo se apaga por separado para medir qué aporta. El argumento que
sostiene el pitch es que cada fila reduce alertas irrelevantes **sin** perder
anticipación: si un mecanismo sólo recorta volumen, no está aportando.

    .venv\\Scripts\\python.exe scripts\\04_ablation.py [--pacientes 300]
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import yaml  # noqa: E402

from hs import paths  # noqa: E402
from hs.detect import EventConfig, barrer, signal_id  # noqa: E402
from hs.domain.scoring import ScoringConfig  # noqa: E402
from hs.timeline import AsOfStore  # noqa: E402

# Predicados de distractor, en función de la ventana de una señal candidata.
MARCADORES = """
    EXISTS (SELECT 1 FROM intervals i
            WHERE i.patient_id = c.patient_id
              AND ((i.kind='CONTEXT' AND (i.subtype='RECOVERY_PHASE'
                    OR (i.subtype='PHYSICAL_ACTIVITY' AND i.value_text IN ('HIGH','MODERATE'))))
                   OR i.kind='CONNECTIVITY')
              AND i.start_time <= c.evidence_end AND i.end_time >= c.evidence_start)
 OR EXISTS (SELECT 1 FROM observations o
            WHERE o.patient_id = c.patient_id
              AND (NOT o.is_plausible OR o.is_duplicate OR o.quality_flag='LOW_SIGNAL')
              AND o.event_time BETWEEN c.evidence_start AND c.evidence_end)
"""


def variantes(base: dict) -> list[tuple[str, dict]]:
    """Del motor desnudo al completo, agregando un mecanismo por vez."""
    def sin_concordancia(d):
        d["puntaje"]["concordancia"] = {k: 1.0 for k in (1, 2, 3, 4, 5, 6)}
        return d

    def sin_persistencia(d):
        d["puntaje"]["umbral_persistencia"] = -1e9      # persistencia siempre 1
        return d

    def sin_supresion(d):
        d["supresion"]["activas"] = False
        return d

    def sin_techo(d):
        # Sin recorte por canal, la magnitud de uno solo domina la suma. Es el
        # motor que el principio P-07 prohíbe, y la referencia contra la cual
        # se mide todo lo demás.
        d["puntaje"]["techo_por_canal"] = 1e9
        return d

    v = []
    d = sin_techo(sin_supresion(sin_persistencia(sin_concordancia(copy.deepcopy(base)))))
    v.append(("0 · magnitud sin recorte", d))
    d = sin_supresion(sin_persistencia(sin_concordancia(copy.deepcopy(base))))
    v.append(("1 · + recorte por canal", d))
    d = sin_supresion(sin_concordancia(copy.deepcopy(base)))
    v.append(("2 · + persistencia", d))
    d = sin_supresion(copy.deepcopy(base))
    v.append(("3 · + concordancia", d))
    v.append(("4 · + supresión (completo)", copy.deepcopy(base)))
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pacientes", type=int, default=300)
    args = ap.parse_args()

    base = yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8"))
    # Base en memoria con el almacén adjunto de sólo lectura: la ablación no
    # necesita escribir nada y así no bloquea a la API ni a otra corrida.
    con = duckdb.connect()
    con.execute(f"ATTACH '{str(paths.WAREHOUSE).replace(chr(92), '/')}' AS wh (READ_ONLY)")
    con.execute("USE wh")
    cohorte = AsOfStore(con).patients()[:args.pacientes]
    con.execute("USE memory")
    for t in ("observations", "intervals", "clinical_facts", "patients"):
        con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM wh.{t}")

    con.execute("""
        CREATE OR REPLACE TABLE cand (
            variante VARCHAR, signal_id VARCHAR, patient_id VARCHAR,
            decision_datetime TIMESTAMP, evidence_start TIMESTAMP,
            evidence_end TIMESTAMP, risk_score DOUBLE, priority_level VARCHAR,
            supresiones VARCHAR, canal_dominante VARCHAR, k INTEGER)
    """)

    print(f"Ablación sobre {len(cohorte)} pacientes\n")
    for nombre, d in variantes(base):
        cfg, ev = ScoringConfig.from_dict(d), EventConfig.from_dict(d)
        store = AsOfStore(con, lookback=cfg.evidencia + cfg.baseline)
        señales, st = barrer(store, cfg, ev, pacientes=cohorte)
        filas = []
        for a in señales:
            dom = a.dominante()
            filas.append((nombre, signal_id(a), a.patient_id, a.T, a.evidence_start,
                          a.evidence_end, a.risk, a.priority,
                          ",".join(s.regla for s in a.supresiones),
                          dom.variable_code if dom else None, a.k))
        if filas:
            con.executemany("INSERT INTO cand VALUES (?,?,?,?,?,?,?,?,?,?,?)", filas)
        print(f"  {nombre:32} {len(señales):5,} señales   "
              f"({st.evaluaciones:,} evaluaciones)")

    print("\n" + "=" * 104)
    print(f"{'configuración':32} {'HIGH+':>7} {'de 1 canal':>11} {'tasa':>7} "
          f"{'distractor':>11} {'anticip.':>9} {'pacientes':>10}")
    print("=" * 104)
    for nombre, _ in variantes(base):
        r = con.execute(f"""
            WITH c AS (SELECT * FROM cand WHERE variante = ?
                       AND priority_level IN ('HIGH','CRITICAL'))
            SELECT count(*),
                   count(*) FILTER (WHERE ({MARCADORES}) AND coalesce(supresiones,'') = ''),
                   count(DISTINCT patient_id),
                   count(*) FILTER (WHERE k <= 1)
            FROM c
        """, [nombre]).fetchone()
        altas, malas, pac, solos = r
        ant = con.execute("""
            WITH c AS (SELECT * FROM cand WHERE variante = ?
                       AND priority_level IN ('HIGH','CRITICAL')),
            pico AS (
              SELECT c.signal_id, c.decision_datetime,
                     arg_max(o.event_time, CASE WHEN c.canal_dominante='SpO2'
                                                THEN -o.value_num ELSE o.value_num END) AS t
              FROM c JOIN observations o
                ON o.patient_id = c.patient_id AND o.variable_code = c.canal_dominante
               AND o.is_plausible AND NOT o.is_duplicate
               AND o.event_time > c.decision_datetime
               AND o.event_time <= c.decision_datetime + INTERVAL 24 HOUR
              GROUP BY 1,2)
            SELECT round(median(epoch(t - decision_datetime)/3600), 1) FROM pico
        """, [nombre]).fetchone()[0]
        tasa = f"{100 * solos / altas:.1f}%" if altas else "—"
        print(f"{nombre:32} {altas:7,} {solos:11,} {tasa:>7} {malas:11,} "
              f"{(str(ant) + ' h') if ant else '—':>9} {pac:10,}")

    print("\n  de 1 canal = señales HIGH+ sustentadas en un solo canal desviado.")
    print("               Es el modo de falla que el principio P-07 prohíbe: magnitud")
    print("               sin corroboración. Bajar esa fracción sin perder anticipación")
    print("               es exactamente lo que la concordancia debe comprar.")
    print("  distractor = coincide con un marcador conocido SIN dejar constancia de evaluarlo")
    print("  anticip.   = mediana de horas hasta el máximo posterior del canal dominante")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
