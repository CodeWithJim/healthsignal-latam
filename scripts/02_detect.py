"""Etapas 4-7: barrido, eventización, exportación y auditoría.

    .venv\\Scripts\\python.exe scripts\\02_detect.py [--pacientes N]

Produce results/signals.csv y results/evidence.csv, y audita la salida sin
confiar en el motor que la produjo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import yaml  # noqa: E402

from hs import ingest, paths  # noqa: E402
from hs.detect import EventConfig, barrer, signal_id  # noqa: E402
from hs.domain.scoring import ScoringConfig  # noqa: E402
from hs.export import auditar_causalidad, auditar_huerfanas, exportar, guardar  # noqa: E402
from hs.timeline import AsOfStore  # noqa: E402


def rule(t: str) -> None:
    print("\n" + "=" * 80)
    print(t)
    print("=" * 80)


def pct(v: list[float], p: float) -> float:
    return v[min(len(v) - 1, int(len(v) * p))] if v else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pacientes", type=int, default=0, help="limitar la cohorte")
    ap.add_argument("--warmup", type=float, default=None,
                    help="horas de arranque antes del primer instante evaluable")
    ap.add_argument("--no-export", action="store_true",
                    help="no escribir results/: para comparar configuraciones")
    args = ap.parse_args()

    d = yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8"))
    if args.warmup is not None:
        d["eventizacion"]["warmup_h"] = args.warmup
    cfg, ev = ScoringConfig.from_dict(d), EventConfig.from_dict(d)

    con = ingest.connect()          # aplica el esquema: incluye migraciones
    store = AsOfStore(con, lookback=cfg.evidencia + cfg.baseline)
    cohorte = store.patients()
    if args.pacientes:
        cohorte = cohorte[:args.pacientes]

    run_id = "det-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    rule(f"BARRIDO  ·  {len(cohorte):,} pacientes  ·  cadencia {ev.cadencia}  ·  "
         f"warmup {ev.warmup}  ·  {cfg.model_version}")
    t0 = time.time()

    def progreso(i, n, s):
        el = time.time() - t0
        print(f"  {i:5,}/{n:,}  señales {s:5,}  {el:6.1f}s  "
              f"(ETA {el / i * (n - i):5.0f}s)", flush=True)

    señales, st = barrer(store, cfg, ev, pacientes=cohorte, progreso=progreso)
    dur = time.time() - t0

    rule("BARRIDO — resumen")
    print(f"  evaluaciones           {st.evaluaciones:,}")
    print(f"  con canales evaluables {st.con_canales:,} ({st.con_canales / max(1, st.evaluaciones):.0%})")
    print(f"  tiempo                 {dur / 60:.1f} min ({1000 * dur / max(1, st.evaluaciones):.2f} ms c/u)")
    print(f"  señales emitidas       {len(señales):,}")
    print(f"\n  canales concordantes:  " +
          "  ".join(f"k={k}:{v:,}" for k, v in sorted(st.por_k.items())))
    print(f"  supresiones activadas: {st.supresiones or 'ninguna'}")

    b, r = sorted(st.brutos), sorted(st.riesgos)
    print(f"\n  puntaje bruto: p50={pct(b, .5):.2f} p90={pct(b, .9):.2f} "
          f"p99={pct(b, .99):.2f} p999={pct(b, .999):.2f} max={b[-1] if b else 0:.2f}")
    print(f"  riesgo:        p50={pct(r, .5):.3f} p90={pct(r, .9):.3f} "
          f"p99={pct(r, .99):.3f} p999={pct(r, .999):.3f} max={r[-1] if r else 0:.3f}")

    rule("SEÑALES EMITIDAS")
    por_prio: dict[str, int] = {}
    for a in señales:
        por_prio[a.priority] = por_prio.get(a.priority, 0) + 1
    for p in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        n = por_prio.get(p, 0)
        print(f"  {p:9} {n:5,}   {'█' * min(60, n * 60 // max(1, len(señales)))}")
    print(f"\n  pacientes distintos: {len({a.patient_id for a in señales}):,}")
    print(f"  con supresión:       {sum(1 for a in señales if a.supresiones):,}")
    print(f"  con multifuente:     {sum(1 for a in señales if a.multifuente > 0):,}")

    rule("TOP 10 POR RIESGO")
    for a in sorted(señales, key=lambda x: -x.risk)[:10]:
        canales = " ".join(f"{c.variable_code}{c.s:+.1f}" for c in
                           sorted(a.canales.values(), key=lambda c: -c.s)[:4] if c.s > 0)
        print(f"  {signal_id(a):26} {a.priority:8} risk={a.risk:.3f} conf={a.confidence:.2f} "
              f"k={a.k}  {canales}")

    rule("EXPORTACIÓN")
    ns, ne = guardar(con, señales, run_id)
    if args.no_export:
        print("  (--no-export) tablas actualizadas, results/ sin tocar")
        sp = ep = paths.RESULTS / "(no escrito)"
    else:
        sp, ep = exportar(con, paths.RESULTS)
    print(f"  signals.csv    {ns:,} filas   {sp}")
    print(f"  evidence.csv   {ne:,} filas   {ep}")
    print(f"  evidencia por señal: {ne / max(1, ns):.1f} en promedio")
    for rol, n in con.execute(
            "SELECT evidence_role, count(*) FROM evidence GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"    {rol:12} {n:6,}")

    rule("AUDITORÍA — sin confiar en el motor")
    fugas = auditar_causalidad(con)
    print(f"  CE-01 causalidad temporal: {len(fugas)} violación(es)  "
          f"{'OK' if not fugas else 'FALLA'}")
    for f in fugas[:3]:
        print(f"        {f}")
    sin_ev, huerf = auditar_huerfanas(con)
    print(f"  CE-02 cobertura evidencia: {sin_ev} señal(es) sin evidencia  "
          f"{'OK' if not sin_ev else 'FALLA'}")
    print(f"        {huerf} fila(s) de evidencia huérfana  {'OK' if not huerf else 'FALLA'}")

    reales = con.execute("""
        SELECT count(*) FROM evidence e
        WHERE NOT EXISTS (SELECT 1 FROM observations o
                          WHERE o.source_file = e.source_file AND o.record_id = e.record_id)
          AND NOT EXISTS (SELECT 1 FROM intervals i
                          WHERE i.source_file = e.source_file AND i.record_id = e.record_id)
    """).fetchone()[0]
    print(f"  RF-15 trazabilidad:        {reales} record_id inexistente(s)  "
          f"{'OK' if not reales else 'FALLA'}")

    print(f"\n{'=' * 80}\nrun_id {run_id} · {dur / 60:.1f} min")
    con.close()
    return 0 if not (fugas or sin_ev or huerf or reales) else 1


if __name__ == "__main__":
    raise SystemExit(main())
