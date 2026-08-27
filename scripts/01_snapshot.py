"""Etapa 3 del pipeline: un paciente recorrido de punta a punta a través del puerto.

    .venv\\Scripts\\python.exe scripts\\01_snapshot.py [PAT-0869] [2026-07-20T18:00:00]

Muestra exactamente lo que el motor de decisión verá en un instante dado, y —lo
que importa— lo que existe en la base pero queda fuera de su alcance.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import yaml  # noqa: E402

from hs import paths  # noqa: E402
from hs.timeline import AsOfStore  # noqa: E402

PID = sys.argv[1] if len(sys.argv) > 1 else "PAT-0869"
T = dt.datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 \
    else dt.datetime(2026, 7, 20, 18, 0, 0)


def rule(t: str) -> None:
    print("\n" + "=" * 88)
    print(t)
    print("=" * 88)


def main() -> int:
    cfg = yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8"))
    nominal = cfg["muestreo_nominal_min"]
    w_ev = dt.timedelta(hours=cfg["ventanas"]["evidencia_horas"])
    w_bl = dt.timedelta(hours=cfg["ventanas"]["baseline_horas"])

    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    store = AsOfStore(con, lookback=w_ev + w_bl)

    ev0, ev1 = T - w_ev, T
    bl0, bl1 = T - w_ev - w_bl, T - w_ev

    rule(f"PACIENTE {PID}   ·   DECISIÓN EN {T}")
    win = store.encounter_window(PID)
    print(f"  encuentro           {win[0]}  →  {win[1]}")
    print(f"  ventana evidencia   {ev0}  →  {ev1}")
    print(f"  ventana baseline    {bl0}  →  {bl1}   (termina donde empieza la evidencia)")

    snap = store.snapshot(PID, T)
    print(f"\n  {snap!r}")
    print(f"  dentro del encuentro: {snap.within_encounter()}")

    rule("LO QUE EL MOTOR VE  ·  por canal")
    print(f"  {'canal':14} {'n':>5} {'último':>9} {'cobertura ev':>13} {'cobertura bl':>13} "
          f"{'implaus.':>9}")
    print("  " + "-" * 74)
    for code in ("HR", "RR", "SpO2", "TEMP", "SBP", "DBP", "WEARABLE_HR", "ACTIVITY_LEVEL"):
        s = snap.channel(code)
        if not len(s):
            print(f"  {code:14} {'—':>5}")
            continue
        nm = nominal.get(code, 20)
        vivo = s.present()
        cov_ev = vivo.coverage(ev0, ev1, nm)
        cov_bl = vivo.coverage(bl0, bl1, nm)
        if s.is_categorical:
            ult = vivo.slice(ev0, ev1)
            val = ult.texts[-1] if len(ult) else "—"
        else:
            last = vivo.last()
            val = f"{last[1]:.1f}" if last else "—"
        print(f"  {code:14} {len(s):5} {val:>9} {cov_ev:12.0%} {cov_bl:12.0%} "
              f"{len(s.implausible()):9}")

    rule("LO QUE EXISTE PERO NO PUEDE USAR")
    oculto = con.execute("""
        SELECT variable_code, record_id, event_time, available_time
        FROM observations
        WHERE patient_id = ? AND NOT is_duplicate
          AND event_time <= ? AND available_time > ?
        ORDER BY available_time
    """, [PID, T, T]).fetchall()
    if oculto:
        for code, rid, et, av in oculto[:8]:
            espera = (av - et).total_seconds() / 60
            print(f"  {code:12} {rid:16} ocurrió {et}  disponible {av}  (+{espera:.0f} min)")
        print(f"\n  {len(oculto)} hecho(s) ya ocurridos que todavía no estaban informados en T.")
    else:
        print("  Ninguno en este instante para este paciente.")

    print("\n  Verificación: ninguno de esos record_id aparece en el snapshot.")
    vistos = {r for s in snap.series.values() for r in s.record_ids}
    fuga = [r for _, r, _, _ in oculto if r in vistos]
    print(f"  filtrados correctamente: {len(oculto) - len(fuga)}/{len(oculto)}   "
          f"{'OK' if not fuga else 'FUGA: ' + str(fuga)}")

    rule("CONTEXTO DISPONIBLE EN T")
    ctx = snap.intervals_overlapping(ev0, ev1)
    if ctx:
        for iv in ctx:
            estado = "en curso" if iv.ongoing_at(T) else "cerrado"
            fin = iv.end_as_of(T)
            nota = "  ← fin recortado en T" if iv.ongoing_at(T) else ""
            print(f"  {iv.kind:13} {iv.subtype or '':18} {str(iv.value_text or ''):22} "
                  f"{iv.start} → {fin}  [{estado}]{nota}")
    else:
        print("  Sin intervalos de contexto solapando la ventana de evidencia.")

    if snap.facts:
        print(f"\n  antecedentes disponibles: "
              f"{', '.join(f.category or '?' for f in snap.facts)}")

    if snap.excluded:
        rule("FILAS APARTADAS DEL CÁLCULO, CITABLES COMO QUALITY")
        for x in snap.excluded[:6]:
            print(f"  {x.reason:11} {x.record_id:16} {x.variable_code:6} {x.event_time}")

    rule("TRAYECTORIA EN LA VENTANA DE EVIDENCIA")
    hr, spo2 = snap.window("HR", ev0, ev1).usable(), snap.window("SpO2", ev0, ev1).usable()
    rr, temp = snap.window("RR", ev0, ev1).usable(), snap.window("TEMP", ev0, ev1).usable()
    print(f"\n  {'hora':>6}   {'HR':>7} {'RR':>7} {'SpO2':>7} {'TEMP':>7}")
    for i in range(len(hr)):
        t = hr.times[i].astype(dt.datetime)
        def near(s):
            if not len(s):
                return "—"
            j = int(abs(s.times - hr.times[i]).argmin())
            return f"{s.values[j]:.1f}" if abs(
                (s.times[j].astype(dt.datetime) - t).total_seconds()) <= 1800 else "—"
        print(f"  {t.strftime('%H:%M'):>6}   {hr.values[i]:7.1f} {near(rr):>7} "
              f"{near(spo2):>7} {near(temp):>7}")

    print(f"\n{'=' * 88}")
    print("El motor recibirá exactamente este objeto. No tiene forma de pedir nada más.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
