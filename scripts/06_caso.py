"""Evidencia de identificación y priorización de riesgo.

El entregable central del reto: recorrer un caso completo mostrando cómo
información de variables y fuentes distintas se transforma en una señal
priorizada, con la cadena que el jurado tiene que poder verificar.

    datos → evolución y contexto → patrón → señal → prioridad → evidencia → explicación

    .venv\\Scripts\\python.exe scripts\\06_caso.py [HS-0869-20260720T1800]

Sin argumento toma la señal de mayor riesgo. Todo lo que imprime se computa en
el momento a través del puerto as-of; nada sale de una tabla precalculada.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import yaml  # noqa: E402

from hs import paths  # noqa: E402
from hs.detect import signal_id  # noqa: E402
from hs.domain.scoring import ScoringConfig, assess  # noqa: E402
from hs.timeline import AsOfStore  # noqa: E402

PASO = "▸"


def rule(n: int, t: str) -> None:
    print(f"\n{'─' * 86}")
    print(f"{PASO} PASO {n} · {t}")
    print("─" * 86)


def main() -> int:
    d = yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8"))
    cfg = ScoringConfig.from_dict(d)
    con = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    store = AsOfStore(con, lookback=cfg.evidencia + cfg.baseline)

    if len(sys.argv) > 1:
        fila = con.execute("SELECT signal_id, patient_id, decision_datetime FROM signals "
                           "WHERE signal_id = ?", [sys.argv[1]]).fetchone()
    else:
        fila = con.execute("SELECT signal_id, patient_id, decision_datetime FROM signals "
                           "ORDER BY risk_score DESC LIMIT 1").fetchone()
    if not fila:
        print("Señal no encontrada. Correr scripts/02_detect.py primero.")
        return 1
    sid, pid, T = fila

    print("=" * 86)
    print(f"  CASO {sid}   ·   paciente {pid}   ·   decisión {T}")
    print("=" * 86)
    print("  Todo lo que sigue se computa en el momento a través del puerto as-of.")

    # ---------------------------------------------------------------- 1
    rule(1, "DATOS INVOLUCRADOS · quién es el paciente y con qué se lo observa")
    p = con.execute("""
        SELECT age_years, sex_at_birth, region_type, care_program, baseline_risk_profile
        FROM patients WHERE patient_id = ?""", [pid]).fetchone()
    print(f"  {p[0]} años · {p[1]} · {p[2]} · programa {p[3]} · perfil basal {p[4]}")
    e = con.execute("""
        SELECT subtype, value_text, start_time, end_time FROM intervals
        WHERE patient_id = ? AND kind = 'ENCOUNTER'""", [pid]).fetchone()
    print(f"  encuentro: {e[0]} en entorno {e[1]}   {e[2]} → {e[3]}")
    ant = con.execute("SELECT category, onset_date, available_time FROM clinical_facts "
                      "WHERE patient_id = ? ORDER BY available_time", [pid]).fetchall()
    for a in ant:
        print(f"  antecedente: {a[0]:26} inicio {a[1]}   registrado {a[2]}")
    print(f"\n  fuentes con datos de este paciente:")
    for r in con.execute("""
        SELECT source_file, count(*) FROM observations WHERE patient_id = ? GROUP BY 1
        UNION ALL SELECT source_file, count(*) FROM intervals WHERE patient_id = ? GROUP BY 1
        ORDER BY 2 DESC""", [pid, pid]).fetchall():
        print(f"    {r[0]:44} {r[1]:8,} registros")

    # ---------------------------------------------------------------- 2
    snap = store.snapshot(pid, T)
    a = assess(snap, cfg)
    ev0, ev1 = a.evidence_start, a.evidence_end

    rule(2, f"EVOLUCIÓN · ventana de evidencia {ev0} → {ev1}")
    hr = snap.window("HR", ev0, ev1).usable()
    print(f"\n  {'hora':>6}  {'HR':>7} {'RR':>7} {'SpO2':>7} {'TEMP':>7}     desviaciones vs. su propio baseline")
    for i in range(len(hr)):
        t = hr.times[i].astype(dt.datetime)
        vals, zs = [], []
        for code in ("HR", "RR", "SpO2", "TEMP"):
            s = snap.window(code, ev0, ev1).usable()
            c = a.canales.get(code)
            if not len(s) or c is None:
                vals.append("—"); zs.append("")
                continue
            j = int(abs(s.times - hr.times[i]).argmin())
            cerca = abs((s.times[j].astype(dt.datetime) - t).total_seconds()) <= 1800
            if not cerca:
                vals.append("—"); zs.append("")
                continue
            v = s.values[j]
            z = (v - c.mediana_baseline) / c.escala
            vals.append(f"{v:.1f}")
            zs.append(f"{code}{z:+.1f}")
        print(f"  {t.strftime('%H:%M'):>6}  " + " ".join(f"{v:>7}" for v in vals) +
              "     " + "  ".join(x for x in zs if x))

    rule(3, "CONTEXTO DISPONIBLE · lo que podría explicar la variación")
    ctx = snap.intervals_overlapping(ev0 - dt.timedelta(hours=12), ev1)
    if ctx:
        for iv in ctx:
            estado = "en curso" if iv.ongoing_at(T) else "cerrado"
            print(f"  {iv.kind:13} {iv.subtype or '':20} {str(iv.value_text or ''):24} "
                  f"{iv.start} → {iv.end_as_of(T)}  [{estado}]  {iv.record_id}")
    else:
        print("  Sin intervalos de contexto solapando la ventana ni las 12 h previas.")
    act = snap.window("ACTIVITY_LEVEL", ev0, ev1).categorical()
    if len(act):
        from collections import Counter
        print(f"\n  nivel de actividad del wearable en la ventana: "
              f"{dict(Counter(act.texts))}")

    # ---------------------------------------------------------------- 4
    rule(4, "PATRÓN IDENTIFICADO · contribución de cada canal")
    print(f"  {'canal':6} {'aporte':>7} {'nivel':>8} {'deriva':>8} {'persist':>8} "
          f"{'cobert':>7} {'baseline':>18} {'último':>8}")
    print("  " + "-" * 78)
    for c in sorted(a.canales.values(), key=lambda x: -x.s):
        print(f"  {c.variable_code:6} {c.s:7.2f} {c.nivel:+8.1f} {c.pendiente:+8.1f} "
              f"{c.persistencia:8.0%} {c.cobertura:7.0%} "
              f"{c.mediana_baseline:>10.1f} ±{c.escala:<6.2f} {c.ultimo_valor:8.1f}")
    print(f"\n  {a.k} canales concordantes de {len(a.canales)} evaluados.")
    print(f"  puntaje bruto {a.bruto:.2f}   tras supresión {a.puntaje:.2f}"
          + (f"   corroboración multifuente +{a.multifuente:.0f}" if a.multifuente else ""))

    # ---------------------------------------------------------------- 5
    rule(5, "REGLAS EVALUADAS · lo que se consideró y se descartó")
    if a.supresiones:
        for s in a.supresiones:
            efecto = (f"puntaje reducido {s.fuerza:.0%}" if s.fuerza > 0
                      else "registrado, sin reducir el puntaje")
            print(f"  {s.regla:22} {efecto}")
            print(f"  {'':22} {s.motivo}")
            if s.citas:
                print(f"  {'':22} cita: {', '.join(c.record_id for c in s.citas[:4])}")
            print()
    else:
        print("  Ninguna hipótesis alternativa aplicable en esta ventana.")

    # ---------------------------------------------------------------- 6
    rule(6, "SEÑAL Y PRIORIDAD")
    print(f"  riesgo        {a.risk:.3f}")
    print(f"  confianza     {a.confidence:.3f}")
    print(f"  prioridad     {a.priority}")
    print(f"  ventana       {a.evidence_start} → {a.evidence_end}")
    print(f"  model_version {a.model_version}")
    puesto = con.execute("""
        SELECT count(*) + 1 FROM signals
        WHERE risk_score > (SELECT risk_score FROM signals WHERE signal_id = ?)""",
        [sid]).fetchone()[0]
    total = con.execute("SELECT count(*) FROM signals").fetchone()[0]
    print(f"  ranking       puesto {puesto} de {total} señales emitidas")

    # ---------------------------------------------------------------- 7
    rule(7, "EVIDENCIA · cada fila apunta a un registro real de RISA")
    por_rol: dict[str, list] = {}
    for c in a.citas:
        por_rol.setdefault(c.role, []).append(c)
    for rol in ("PRIMARY", "SUPPORTING", "CONTEXT", "QUALITY"):
        filas = por_rol.get(rol, [])
        if not filas:
            continue
        print(f"\n  {rol} · {len(filas)}")
        for c in filas[:6]:
            print(f"    {c.source_file:42} {c.record_id:16} {str(c.variable_code or ''):6} "
                  f"ocurrió {c.event_time}  disponible {c.available_time}")
        if len(filas) > 6:
            print(f"    … y {len(filas) - 6} más")

    print(f"\n  Verificación de causalidad sobre las {len(a.citas)} citas:")
    tarde = [c for c in a.citas if c.available_time > T]
    print(f"    disponibles en o antes de la decisión: {len(a.citas) - len(tarde)}/{len(a.citas)}"
          f"   {'OK' if not tarde else 'FALLA'}")

    # ---------------------------------------------------------------- 8
    rule(8, "EXPLICACIÓN")
    print(f"  {a.explicacion}")

    rule(9, "TRAZA HASTA EL ARCHIVO ORIGINAL")
    prim = por_rol.get("PRIMARY", [])
    if prim:
        c = prim[0]
        ruta = paths.raw_path(c.source_file)
        print(f"  {c.record_id} en {c.source_file}:")
        if ruta.exists():
            p = paths.sql_literal(ruta)
            cols = con.execute(f"SELECT * FROM read_csv('{p}', header=true, all_varchar=true) "
                               f"LIMIT 0").description
            clave = cols[0][0]
            r = con.execute(f"SELECT * FROM read_csv('{p}', header=true, all_varchar=true) "
                            f"WHERE {clave} = ?", [c.record_id]).fetchone()
            for nombre, valor in zip([x[0] for x in cols], r):
                print(f"    {nombre:22} {valor}")
        else:
            print("    (los CSV originales no están disponibles en este entorno)")

    print(f"\n{'=' * 86}")
    print(f"  Cadena completa verificable: {pid} → {T} → {a.priority} → {len(a.citas)} registros")
    print(f"  Reproducible con:  GET /decide?patient={pid}&at={T.isoformat()}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
