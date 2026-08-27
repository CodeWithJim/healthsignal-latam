"""CA-07 — el motor no puede ver el futuro.

Estas pruebas son el criterio de aceptación de la Fase 1. No comprueban que el
código *se acuerde* de filtrar: comprueban que la única vía de lectura del
dominio no puede devolver información posterior a la decisión, y que un snapshot
construido a mano con una violación falla al crearse.
"""
from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pytest

from hs import ingest, paths
from hs.domain import CausalityError, Interval, PatientSnapshot, Series
from hs.timeline import AsOfStore

T0 = dt.datetime(2026, 7, 20, 18, 0, 0)


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("Falta el almacén: correr scripts/00_ingest.py")
    c = duckdb.connect(str(paths.WAREHOUSE), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def store(con):
    return AsOfStore(con)


# ------------------------------------------------------------------ el caso claro

def test_un_laboratorio_tomado_antes_pero_informado_despues_no_es_visible(con, store):
    """La latencia de laboratorio (mediana 133 min) crea esta situación a diario."""
    row = con.execute("""
        SELECT patient_id, record_id, variable_code, event_time, available_time
        FROM observations
        WHERE domain = 'LAB' AND event_time <= ? AND available_time > ?
        ORDER BY available_time LIMIT 1
    """, [T0, T0]).fetchone()
    assert row, "se esperaba al menos un laboratorio a caballo de T"
    pid, rid, code, event_time, available_time = row
    assert event_time <= T0 < available_time

    snap = store.snapshot(pid, T0)
    citables = {r for s in snap.series.values() for r in s.record_ids}
    assert rid not in citables, (
        f"{rid} se tomó en {event_time} pero recién estuvo disponible en "
        f"{available_time}: no puede sustentar una decisión en {T0}")

    # y sí aparece una vez que su resultado está informado
    luego = store.snapshot(pid, available_time)
    assert rid in {r for s in luego.series.values() for r in s.record_ids}


def test_el_wearable_no_es_visible_antes_de_sincronizar(con, store):
    row = con.execute("""
        SELECT patient_id, record_id, event_time, available_time
        FROM observations
        WHERE domain = 'WEARABLE' AND available_time > event_time
        ORDER BY (available_time - event_time) DESC LIMIT 1
    """).fetchall()[0]
    pid, rid, event_time, available_time = row
    justo_antes = available_time - dt.timedelta(seconds=1)
    antes = store.snapshot(pid, justo_antes)
    assert rid not in {r for s in antes.series.values() for r in s.record_ids}
    despues = store.snapshot(pid, available_time)
    assert rid in {r for s in despues.series.values() for r in s.record_ids}


# ------------------------------------------------------------------ barrido real

def test_invariante_sobre_una_muestra_amplia_de_pacientes_e_instantes(store):
    """Cientos de snapshots reales: ninguno contiene un hecho posterior a su T."""
    pacientes = store.patients()[::37]           # ~27 pacientes repartidos
    revisados = 0
    for pid in pacientes:
        win = store.encounter_window(pid)
        if not win:
            continue
        start, end = win
        for frac in (0.25, 0.5, 0.75, 1.0):
            T = start + (end - start) * frac
            snap = store.snapshot(pid, T)
            snap.assert_causal()                  # explícito, además del constructor
            t64 = np.datetime64(T, "us")
            for s in snap.series.values():
                assert s.available.max() <= t64
            for iv in snap.intervals:
                assert iv.available <= T
            for f in snap.facts:
                assert f.available <= T
            revisados += 1
    assert revisados >= 80, f"sólo se revisaron {revisados} snapshots"


def test_la_ventana_respeta_el_lookback(store):
    pid = store.patients()[0]
    win = store.encounter_window(pid)
    T = win[0] + dt.timedelta(hours=60)
    lb = dt.timedelta(hours=54)
    snap = store.snapshot(pid, T, lookback=lb)
    piso = np.datetime64(T - lb, "us")
    for s in snap.series.values():
        assert s.times.min() >= piso


# ------------------------------------------------------------------ la invariante misma

def test_el_snapshot_rechaza_una_serie_con_futuro():
    T = dt.datetime(2026, 7, 10, 12, 0, 0)
    s = Series(
        "HR",
        np.array([np.datetime64(T - dt.timedelta(minutes=20), "us")]),
        np.array([np.datetime64(T + dt.timedelta(minutes=1), "us")]),   # disponible después
        np.array([88.0]), (None,), ("OBS-X",), ("f.csv",), np.array([True]), "bpm",
    )
    with pytest.raises(CausalityError):
        PatientSnapshot("PAT-0001", T, {"HR": s})


def test_el_snapshot_rechaza_un_intervalo_con_futuro():
    T = dt.datetime(2026, 7, 10, 12, 0, 0)
    iv = Interval("c.csv", "CONN-X", "CONNECTIVITY", "DISCONNECTED", "DISCONNECTED",
                  T + dt.timedelta(hours=1), T + dt.timedelta(hours=3),
                  T + dt.timedelta(hours=1))
    with pytest.raises(CausalityError):
        PatientSnapshot("PAT-0001", T, {}, (iv,))


def test_un_intervalo_en_curso_no_revela_su_fin(store, con):
    """Saber que el sueño termina a las 06:00 cuando son las 01:00 es futuro."""
    row = con.execute("""
        SELECT patient_id, record_id, start_time, end_time
        FROM intervals WHERE kind='CONTEXT' AND subtype='SLEEP_STATE'
          AND end_time > start_time + INTERVAL 4 HOUR
        ORDER BY start_time LIMIT 1
    """).fetchone()
    pid, rid, start, end = row
    T = start + (end - start) / 2                 # a mitad del intervalo
    snap = store.snapshot(pid, T)
    iv = next(i for i in snap.intervals if i.record_id == rid)
    assert iv.ongoing_at(T)
    assert iv.end_as_of(T) == T, "el fin observable en T es T, no el fin real"
    assert iv.end_as_of(end + dt.timedelta(hours=1)) == end


# ------------------------------------------------------------------ duplicados

def test_las_duplicadas_no_entran_al_calculo_pero_siguen_citables(con, store):
    row = con.execute("""
        SELECT patient_id, record_id, event_time FROM observations
        WHERE is_duplicate ORDER BY event_time LIMIT 1
    """).fetchone()
    pid, rid, event_time = row
    T = event_time + dt.timedelta(hours=6)
    snap = store.snapshot(pid, T)

    en_series = {r for s in snap.series.values() for r in s.record_ids}
    assert rid not in en_series, "una retransmisión duplicada distorsionaría medias y cobertura"

    apartadas = {x.record_id: x for x in snap.excluded}
    assert rid in apartadas, "debe seguir disponible para citarla como QUALITY"
    assert apartadas[rid].reason == "DUPLICATE"


def test_las_implausibles_quedan_marcadas_y_separables(con, store):
    row = con.execute("""
        SELECT patient_id, record_id, variable_code, event_time FROM observations
        WHERE NOT is_plausible AND quality_flag = 'OK' LIMIT 1
    """).fetchone()
    pid, rid, code, event_time = row
    snap = store.snapshot(pid, event_time + dt.timedelta(hours=1))
    s = snap.channel(code)
    assert rid in s.record_ids, "sigue en la serie: es citable"
    assert rid not in s.usable().record_ids, "pero no entra al cálculo"
    assert rid in s.implausible().record_ids


# ------------------------------------------------------------------ corte exacto

def test_corte_exacto_sobre_datos_construidos(tmp_path):
    """Un almacén mínimo con filas a ambos lados de T: se ve exactamente lo esperado."""
    c = ingest.connect(tmp_path / "probe.duckdb")
    T = dt.datetime(2026, 7, 10, 12, 0, 0)
    filas = [
        # (record_id, event_time, available_time, esperado_visible)
        ("A", T - dt.timedelta(hours=1), T - dt.timedelta(hours=1), True),
        ("B", T,                          T,                         True),
        ("C", T - dt.timedelta(minutes=30), T + dt.timedelta(seconds=1), False),
        ("D", T + dt.timedelta(minutes=20), T + dt.timedelta(minutes=20), False),
        ("E", T - dt.timedelta(hours=80), T - dt.timedelta(hours=80), False),  # fuera del lookback
    ]
    for rid, et, av, _ in filas:
        c.execute(
            "INSERT INTO observations VALUES ('x.csv',?,'PAT-0001',NULL,NULL,'HR','VITAL',"
            "?,?,70.0,NULL,70.0,'bpm','bpm','MONITOR_GATEWAY','OK',TRUE,FALSE,NULL,NULL)",
            [rid, et, av])
    try:
        snap = AsOfStore(c).snapshot("PAT-0001", T)
        visto = set(snap.channel("HR").record_ids)
        assert visto == {rid for rid, _, _, ok in filas if ok}, visto
    finally:
        c.close()


def test_snapshot_es_la_unica_lectura_del_dominio():
    """El puerto expone una sola operación que produce objetos de dominio."""
    publicos = [m for m in dir(AsOfStore) if not m.startswith("_")]
    assert set(publicos) == {"snapshot", "timeline", "patients",
                             "encounter_window", "decision_times"}
    import inspect
    sig = inspect.signature(AsOfStore.snapshot)
    assert "T" in sig.parameters, "snapshot debe exigir el instante de decisión"
    assert sig.return_annotation == "PatientSnapshot"
