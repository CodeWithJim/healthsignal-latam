"""Etapa 0-2 del pipeline: RAW -> CLEAN.

    .venv\\Scripts\\python.exe scripts\\00_ingest.py

Verifica integridad de origen, carga las 17 fuentes al almacén y emite el
informe de verificación de la Fase 0.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hs import ingest, paths  # noqa: E402


def rule(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def main() -> int:
    t0 = time.time()
    con = ingest.connect()
    info = ingest.run(con)

    rule("INTEGRIDAD DE ORIGEN")
    rows = con.execute("""
        SELECT source_file, sha256_ok, bytes, rows_read, rows_loaded, rows_quarantined
        FROM ingest_manifest ORDER BY source_file
    """).fetchall()
    print(f"{'archivo':44} {'sha':>4} {'MB':>7} {'leídas':>10} {'cargadas':>10} {'cuarent.':>9}")
    for sf, ok, b, rd, ld, q in rows:
        mark = "ok" if ok else ("--" if ok is None else "DIFF")
        print(f"{sf:44} {mark:>4} {b/1e6:7.1f} {rd:10,} {ld:10,} {q:9,}")

    n_ok = sum(1 for r in rows if r[1])
    print(f"\n{n_ok}/{len(rows)} archivos coinciden con MANIFEST_SHA256.txt")

    rule("INVARIANTE RF-02  ·  leídas = cargadas + cuarentena")
    bad = con.execute("""
        SELECT source_file, rows_read, rows_loaded, rows_quarantined
        FROM ingest_manifest
        WHERE target_table IN ('observations','intervals','clinical_facts')
          AND rows_read <> rows_loaded + rows_quarantined
    """).fetchall()
    if bad:
        for r in bad:
            print(f"  VIOLADA  {r[0]}: {r[1]:,} <> {r[2]:,} + {r[3]:,}")
    else:
        tot = con.execute("""
            SELECT sum(rows_read), sum(rows_loaded), sum(rows_quarantined)
            FROM ingest_manifest
            WHERE target_table IN ('observations','intervals','clinical_facts')
        """).fetchone()
        print(f"  se cumple en todas las fuentes: {tot[0]:,} = {tot[1]:,} + {tot[2]:,}")

    rule("CAPA CLEAN")
    for t in ("observations", "intervals", "clinical_facts", "quarantine"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:16} {n:12,}")

    rule("P-02  ·  causalidad temporal")
    v = con.execute(
        "SELECT count(*) FROM observations WHERE available_time < event_time"
    ).fetchone()[0]
    print(f"  observaciones con available_time < event_time: {v}   "
          f"({'CHECK activo' if v == 0 else 'VIOLACIÓN'})")
    lag = con.execute("""
        SELECT domain,
               round(min(epoch(available_time - event_time))/60, 1),
               round(median(epoch(available_time - event_time))/60, 1),
               round(max(epoch(available_time - event_time))/60, 1)
        FROM observations GROUP BY domain ORDER BY domain
    """).fetchall()
    print(f"\n  {'dominio':10} {'lag min':>9} {'mediana':>9} {'máx':>9}   (minutos)")
    for d, mn, md, mx in lag:
        print(f"  {d:10} {mn:9} {md:9} {mx:9}")

    rule("RD-04  ·  normalización de unidades")
    for r in con.execute("""
        SELECT variable_code, unit_raw, unit_canonical, count(*),
               round(min(value_num), 2), round(max(value_num), 2)
        FROM observations WHERE variable_code = 'TEMP'
        GROUP BY 1,2,3 ORDER BY 4 DESC
    """).fetchall():
        print(f"  {r[0]:6} {r[1]:>5} -> {r[2]:<5} {r[3]:8,}   rango canónico [{r[4]}, {r[5]}]")

    rule("RD-06  ·  gate de plausibilidad propio")
    print("  implausibles por variable, con el quality_flag que traían de origen:")
    for r in con.execute("""
        SELECT variable_code, coalesce(quality_flag, '<null>'), count(*)
        FROM observations WHERE NOT is_plausible
        GROUP BY 1,2 ORDER BY 3 DESC
    """).fetchall():
        print(f"    {r[0]:6} flag={r[1]:<8} {r[2]:6,}")
    tot = con.execute("SELECT count(*) FROM observations WHERE NOT is_plausible").fetchone()[0]
    okf = con.execute(
        "SELECT count(*) FROM observations WHERE NOT is_plausible AND quality_flag = 'OK'"
    ).fetchone()[0]
    print(f"\n    total {tot:,}, de las cuales {okf:,} venían marcadas OK en el origen")

    rule("RD-05  ·  retransmisiones y deduplicación")
    print(f"  disponibilidad ajustada: {info['adjusted']:,} filas")
    print(f"  marcadas duplicadas:     {info['duplicates']:,} filas")
    for r in con.execute("""
        SELECT source_system, quality_flag, count(*), count(DISTINCT patient_id)
        FROM observations WHERE is_duplicate GROUP BY 1,2
    """).fetchall():
        print(f"    {r[0]} / {r[1]}: {r[2]:,} filas en {r[3]} pacientes")
    dup = con.execute("""
        SELECT count(*) FROM (
            SELECT 1 FROM observations WHERE NOT is_duplicate
            GROUP BY patient_id, variable_code, event_time HAVING count(*) > 1
        )
    """).fetchone()[0]
    print(f"\n  llaves duplicadas entre filas NO marcadas: {dup}   "
          f"({'correcto' if dup == 0 else 'REVISAR'})")

    rule("MATERIA PRIMA DEL MOTOR")
    for r in con.execute("""
        SELECT domain, variable_code, count(*) AS n, count(DISTINCT patient_id) AS pac
        FROM observations WHERE is_plausible AND NOT is_duplicate
        GROUP BY 1,2 ORDER BY 1, 3 DESC
    """).fetchall():
        print(f"  {r[0]:9} {r[1]:22} {r[2]:10,}  en {r[3]:5,} pacientes")
    for r in con.execute(
        "SELECT kind, count(*), count(DISTINCT patient_id) FROM intervals GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  INTERVAL  {r[0]:22} {r[1]:10,}  en {r[2]:5,} pacientes")

    rule("VENTANA DE ESTUDIO (RD-01)")
    r = con.execute("SELECT min(event_time), max(event_time) FROM observations").fetchone()
    print(f"  observaciones: {r[0]}  ->  {r[1]}")
    r = con.execute("SELECT min(start_time), max(end_time) FROM intervals").fetchone()
    print(f"  intervalos:    {r[0]}  ->  {r[1]}")

    print(f"\n{'=' * 78}\nOK — capa CLEAN construida en {time.time() - t0:.1f} s")
    print(f"almacén: {paths.WAREHOUSE}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
