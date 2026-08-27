"""Constructor de trayectorias sintéticas para los criterios de aceptación.

Permite probar el analizador sin tocar un CSV: si el motor no distingue estas
seis formas, no importa qué produzca sobre los datos reales.

El ruido de cada canal usa la dispersión medida en RISA (p10 de la distribución
poblacional de MAD): un paciente estable, que es el supuesto correcto para una
prueba de aceptación.
"""
from __future__ import annotations

import datetime as dt
import zlib
from typing import Callable, Sequence

import numpy as np
import yaml

from hs import paths
from hs.domain.models import DT64, ClinicalFact, ExcludedRow, Interval, PatientSnapshot, Series
from hs.domain.scoring import ScoringConfig

PASO_MIN = {"HR": 20, "RR": 20, "SpO2": 20, "TEMP": 60, "SBP": 120, "DBP": 120}
RUIDO = {"HR": 3.3, "RR": 0.75, "SpO2": 0.37, "TEMP": 0.13, "SBP": 2.8, "DBP": 1.9}
BASAL = {"HR": 88.0, "RR": 18.0, "SpO2": 95.0, "TEMP": 37.1, "SBP": 118.0, "DBP": 75.0}


def config() -> ScoringConfig:
    return ScoringConfig.from_dict(
        yaml.safe_load((paths.CONFIG / "scoring.yaml").read_text(encoding="utf-8")))


def serie(code: str, T: dt.datetime, valor: Callable[[dt.datetime], float], *,
          horas: float = 54.0, seed: int = 0, lag_min: float = 0.0,
          huecos: Sequence[tuple[dt.datetime, dt.datetime]] = (),
          plausible: Callable[[float], bool] | None = None) -> Series:
    """Serie regular en la grilla nominal del canal, con ruido reproducible."""
    paso = PASO_MIN.get(code, 20)
    # crc32 y no hash(): el hash de cadenas de Python está aleatorizado por
    # proceso, así que la misma prueba daría ruido distinto en cada corrida.
    # Un criterio de aceptación que cambia entre ejecuciones no sirve (P-05).
    rng = np.random.default_rng(seed * 1_000_003 + zlib.crc32(code.encode()))
    n = int(horas * 60 / paso) + 1
    marcas = [T - dt.timedelta(minutes=paso * i) for i in range(n)][::-1]
    marcas = [t for t in marcas if not any(a <= t <= b for a, b in huecos)]

    times = np.array([np.datetime64(t, "us") for t in marcas], dtype=DT64)
    avail = np.array([np.datetime64(t + dt.timedelta(minutes=lag_min), "us")
                      for t in marcas], dtype=DT64)
    vals = np.array([valor(t) + rng.normal(0, RUIDO.get(code, 1.0)) for t in marcas])
    plaus = np.array([True if plausible is None else plausible(v) for v in vals], dtype=bool)
    rids = tuple(f"OBS-{code}-{i:05d}" for i in range(len(marcas)))
    return Series(code, times, avail, vals, (None,) * len(marcas), rids,
                  ("03_monitoring/vital_signs.csv",) * len(marcas), plaus, None)


def plano(code: str) -> Callable[[dt.datetime], float]:
    return lambda _t: BASAL[code]


def rampa(code: str, inicio: dt.datetime, destino: float,
          duracion_h: float) -> Callable[[dt.datetime], float]:
    """Basal hasta `inicio`, luego deriva lineal hasta `destino`."""
    base = BASAL[code]

    def f(t: dt.datetime) -> float:
        if t <= inicio:
            return base
        frac = min(1.0, (t - inicio).total_seconds() / (duracion_h * 3600))
        return base + (destino - base) * frac
    return f


def pico(code: str, inicio: dt.datetime, alto: float,
         duracion_min: float) -> Callable[[dt.datetime], float]:
    """Basal, elevación durante `duracion_min`, retorno inmediato."""
    base = BASAL[code]
    fin = inicio + dt.timedelta(minutes=duracion_min)
    return lambda t: alto if inicio <= t <= fin else base


def snapshot(T: dt.datetime, canales: dict[str, Series], *,
             intervalos: Sequence[Interval] = (), apartadas: Sequence[ExcludedRow] = (),
             encuentro: bool = True) -> PatientSnapshot:
    ivs = list(intervalos)
    if encuentro:
        ivs.append(Interval("01_master/encounters.csv", "ENC-TEST", "ENCOUNTER",
                            "HOSPITAL_OBSERVATION", "FACILITY",
                            T - dt.timedelta(days=5), T + dt.timedelta(days=2),
                            T - dt.timedelta(days=5)))
    return PatientSnapshot("PAT-TEST", T, canales, tuple(ivs), (), tuple(apartadas))


def contexto(kind: str, subtype: str, valor: str, ini: dt.datetime, fin: dt.datetime,
             rid: str = "CTX-TEST") -> Interval:
    archivo = ("04_context/connectivity_events.csv" if kind == "CONNECTIVITY"
               else "04_context/patient_context.csv")
    return Interval(archivo, rid, kind, subtype, valor, ini, fin, ini)


def caso_progresivo(T: dt.datetime, inicio_evento: dt.datetime) -> dict[str, Series]:
    """El ejemplo del documento oficial: 08:00 HR 88 / 11:00 HR 108."""
    return {
        "HR":   serie("HR",   T, rampa("HR",   inicio_evento, 108.0, 3), seed=1),
        "RR":   serie("RR",   T, rampa("RR",   inicio_evento,  25.0, 3), seed=2),
        "SpO2": serie("SpO2", T, rampa("SpO2", inicio_evento,  91.0, 3), seed=3),
        "TEMP": serie("TEMP", T, rampa("TEMP", inicio_evento,  38.0, 3), seed=4),
    }


def caso_estable(T: dt.datetime, seed: int = 10) -> dict[str, Series]:
    return {c: serie(c, T, plano(c), seed=seed + i)
            for i, c in enumerate(("HR", "RR", "SpO2", "TEMP"))}
