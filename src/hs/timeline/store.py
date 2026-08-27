"""AsOfStore — el puerto por el que el motor de decisión lee datos.

Es el único punto del sistema donde se aplica el corte temporal, y devuelve
exclusivamente objetos de dominio. Mientras el motor no tenga otra forma de leer,
el temporal leakage no es un error que haya que buscar: es inexpresable (P-02).

`PatientTimeline` vive del lado de infraestructura y contiene el historial
completo, sin corte. No se entrega al dominio: su único uso es producir muchos
snapshots del mismo paciente con una sola consulta.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from ..domain.models import (DT64, ClinicalFact, ExcludedRow, Interval,
                             PatientSnapshot, Series)

DEFAULT_LOOKBACK = dt.timedelta(hours=54)   # baseline 48h + evidencia 6h

_OBS_SQL = """
    SELECT variable_code, event_time, available_time, value_num, value_text,
           record_id, source_file, is_plausible, is_duplicate, unit_canonical
    FROM observations
    WHERE patient_id = ?
    ORDER BY variable_code, event_time
"""

_INT_SQL = """
    SELECT source_file, record_id, kind, subtype, value_text,
           start_time, end_time, available_time, confidence, extra_json
    FROM intervals WHERE patient_id = ? ORDER BY start_time
"""

_FACT_SQL = """
    SELECT source_file, record_id, category, onset_date, available_time, status, severity
    FROM clinical_facts WHERE patient_id = ? ORDER BY available_time
"""


@dataclass
class PatientTimeline:
    """Historial completo de un paciente. Infraestructura, no dominio."""

    patient_id: str
    _series: dict[str, Series]
    _intervals: tuple[Interval, ...]
    _facts: tuple[ClinicalFact, ...]
    _excluded: tuple[ExcludedRow, ...]

    def at(self, T: dt.datetime, lookback: dt.timedelta = DEFAULT_LOOKBACK) -> PatientSnapshot:
        """Corta el historial en T. Único lugar donde se aplica `available <= T`."""
        t64 = np.datetime64(T, "us")
        floor = T - lookback
        f64 = np.datetime64(floor, "us")

        series: dict[str, Series] = {}
        for code, s in self._series.items():
            mask = (s.available <= t64) & (s.times >= f64) & (s.times <= t64)
            cut = s._take(mask)
            if len(cut):
                series[code] = cut

        intervals = tuple(
            iv for iv in self._intervals
            if iv.available <= T and iv.end >= floor and iv.start <= T
        )
        facts = tuple(f for f in self._facts if f.available <= T)
        excluded = tuple(
            x for x in self._excluded
            if x.available_time <= T and floor <= x.event_time <= T
        )
        return PatientSnapshot(self.patient_id, T, series, intervals, facts,
                               excluded, lookback)

    def span(self) -> tuple[dt.datetime | None, dt.datetime | None]:
        lo = hi = None
        for s in self._series.values():
            if not len(s):
                continue
            a = s.times.min().astype(dt.datetime)
            b = s.times.max().astype(dt.datetime)
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        return lo, hi


class AsOfStore:
    """Puerto de lectura. El dominio no conoce ninguna otra fuente de datos."""

    def __init__(self, con, lookback: dt.timedelta = DEFAULT_LOOKBACK):
        self._con = con
        self.lookback = lookback

    # ---- lectura para el dominio

    def snapshot(self, patient_id: str, T: dt.datetime,
                 lookback: dt.timedelta | None = None) -> PatientSnapshot:
        """Estado conocido del paciente en T.

        El corte se aplica dos veces a propósito: en SQL, para no traer de más,
        y en `PatientTimeline.at`, que es la garantía. El constructor de
        `PatientSnapshot` lo verifica una tercera vez.
        """
        lb = lookback or self.lookback
        return self.timeline(patient_id, T=T, lookback=lb).at(T, lb)

    def timeline(self, patient_id: str, *, T: dt.datetime | None = None,
                 lookback: dt.timedelta | None = None) -> PatientTimeline:
        """Historial del paciente. Con T acota la consulta; sin T la trae entera."""
        params: list = [patient_id]
        obs_sql, int_sql, fact_sql = _OBS_SQL, _INT_SQL, _FACT_SQL
        if T is not None:
            lb = lookback or self.lookback
            floor = T - lb
            obs_sql = _OBS_SQL.replace(
                "WHERE patient_id = ?",
                "WHERE patient_id = ? AND available_time <= ? AND event_time BETWEEN ? AND ?")
            int_sql = _INT_SQL.replace(
                "WHERE patient_id = ?",
                "WHERE patient_id = ? AND available_time <= ? AND end_time >= ? AND start_time <= ?")
            fact_sql = _FACT_SQL.replace(
                "WHERE patient_id = ?", "WHERE patient_id = ? AND available_time <= ?")
            obs_params = [patient_id, T, floor, T]
            int_params = [patient_id, T, floor, T]
            fact_params = [patient_id, T]
        else:
            obs_params = int_params = fact_params = params

        rows = self._con.execute(obs_sql, obs_params).fetchall()
        series, excluded = _build_series(rows)
        intervals = tuple(
            Interval(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
            for r in self._con.execute(int_sql, int_params).fetchall()
        )
        facts = tuple(
            ClinicalFact(r[0], r[1], r[2], r[3], r[4], r[5], r[6])
            for r in self._con.execute(fact_sql, fact_params).fetchall()
        )
        return PatientTimeline(patient_id, series, intervals, facts, excluded)

    # ---- utilidades de recorrido

    def patients(self) -> list[str]:
        return [r[0] for r in self._con.execute(
            "SELECT DISTINCT patient_id FROM observations ORDER BY 1").fetchall()]

    def encounter_window(self, patient_id: str) -> tuple[dt.datetime, dt.datetime] | None:
        r = self._con.execute(
            "SELECT start_time, end_time FROM intervals "
            "WHERE patient_id = ? AND kind = 'ENCOUNTER' ORDER BY start_time LIMIT 1",
            [patient_id]).fetchone()
        return (r[0], r[1]) if r else None

    def decision_times(self, patient_id: str, every: dt.timedelta,
                       warmup: dt.timedelta | None = None) -> list[dt.datetime]:
        """Instantes evaluables dentro del encuentro del paciente (RD-09).

        `warmup` descarta el arranque, donde todavía no hay baseline suficiente.
        """
        win = self.encounter_window(patient_id)
        if not win:
            return []
        start, end = win
        t = start + (warmup if warmup is not None else self.lookback)
        out: list[dt.datetime] = []
        while t <= end:
            out.append(t)
            t += every
        return out


# --------------------------------------------------------------------------- helpers

def _build_series(rows) -> tuple[dict[str, Series], tuple[ExcludedRow, ...]]:
    """Agrupa filas por variable en arreglos vectorizados, preservando procedencia.

    Las duplicadas no entran a las series: no aportan información y distorsionan
    medias y cobertura. Se conservan aparte para poder citarlas como QUALITY.
    """
    buckets: dict[str, list] = {}
    excluded: list[ExcludedRow] = []
    for (code, et, av, vnum, vtext, rid, sf, plaus, dup, unit) in rows:
        if dup:
            excluded.append(ExcludedRow(sf, rid, code, et, av, "DUPLICATE"))
            continue
        buckets.setdefault(code, []).append((et, av, vnum, vtext, rid, sf, plaus, unit))

    series: dict[str, Series] = {}
    for code, items in buckets.items():
        n = len(items)
        times = np.empty(n, dtype=DT64)
        avail = np.empty(n, dtype=DT64)
        vals = np.empty(n, dtype=float)
        plaus = np.empty(n, dtype=bool)
        texts: list[str | None] = []
        rids: list[str] = []
        sfs: list[str] = []
        unit = None
        for i, (et, av, vnum, vtext, rid, sf, pl, un) in enumerate(items):
            times[i] = np.datetime64(et, "us")
            avail[i] = np.datetime64(av, "us")
            vals[i] = np.nan if vnum is None else float(vnum)
            plaus[i] = bool(pl)
            texts.append(vtext)
            rids.append(rid)
            sfs.append(sf)
            unit = unit or un
        order = np.argsort(times, kind="stable")
        series[code] = Series(
            code, times[order], avail[order], vals[order],
            tuple(texts[i] for i in order), tuple(rids[i] for i in order),
            tuple(sfs[i] for i in order), plaus[order], unit,
        )
    return series, tuple(excluded)
