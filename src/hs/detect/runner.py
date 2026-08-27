"""Barrido de detección y política de eventización (etapas 4-5 del pipeline).

Evaluar no es emitir. El analizador produce un dictamen en cada instante de la
grilla; sólo los **cambios materiales de estado** se convierten en señal. Sin esa
distinción, signals.csv tendría cientos de miles de filas y ninguna sería
accionable (RF-11).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from ..domain.scoring import Assessment, ScoringConfig, assess

ORDEN = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class EventConfig:
    cadencia: dt.timedelta
    refractario: dt.timedelta
    delta_reemision: float
    emitir_desde: str
    umbral_critical: float
    umbral_high: float
    umbral_medium: float

    @classmethod
    def from_dict(cls, d: dict) -> "EventConfig":
        e, p = d["eventizacion"], d["prioridad"]
        return cls(
            cadencia=dt.timedelta(minutes=float(e["cadencia_min"])),
            refractario=dt.timedelta(hours=float(e["refractario_h"])),
            delta_reemision=float(e["delta_reemision"]),
            emitir_desde=str(e["emitir_desde"]),
            umbral_critical=float(p["CRITICAL"]["risk"]),
            umbral_high=float(p["HIGH"]["risk"]),
            umbral_medium=float(p["MEDIUM"]["risk"]),
        )


@dataclass
class Stats:
    """Resumen compacto del barrido. Cabe en memoria aunque el barrido no."""

    evaluaciones: int = 0
    con_canales: int = 0
    brutos: list[float] = field(default_factory=list)
    riesgos: list[float] = field(default_factory=list)
    por_k: dict[int, int] = field(default_factory=dict)
    supresiones: dict[str, int] = field(default_factory=dict)

    def registrar(self, a: Assessment) -> None:
        self.evaluaciones += 1
        if a.canales:
            self.con_canales += 1
        self.brutos.append(a.bruto)
        self.riesgos.append(a.risk)
        self.por_k[a.k] = self.por_k.get(a.k, 0) + 1
        for s in a.supresiones:
            self.supresiones[s.regla] = self.supresiones.get(s.regla, 0) + 1


def evaluar_paciente(store, pid: str, cfg: ScoringConfig,
                     ev: EventConfig) -> Iterator[Assessment]:
    """Dictámenes del paciente en toda su grilla. Una sola consulta a la base."""
    momentos = store.decision_times(pid, ev.cadencia)
    if not momentos:
        return
    tl = store.timeline(pid)
    lookback = cfg.evidencia + cfg.baseline
    for T in momentos:
        yield assess(tl.at(T, lookback), cfg)


def eventizar(dictamenes: Iterable[Assessment], cfg: ScoringConfig,
              ev: EventConfig) -> list[Assessment]:
    """Convierte una secuencia de dictámenes en las señales que ameritan emitirse.

    Se emite cuando el paciente entra por primera vez a una banda de prioridad o
    escala a una superior, y se reemite en la misma banda sólo tras el período
    refractario y con un incremento material de riesgo.

    Aparte se emiten los casos que una supresión bajó desde una banda alta: son
    la demostración del control de falsas alertas, no ruido (RF-08).
    """
    piso = ORDEN[ev.emitir_desde]
    salida: list[Assessment] = []
    banda_actual = 0
    ultimo_T: dt.datetime | None = None
    ultimo_risk = 0.0

    for a in dictamenes:
        banda = ORDEN[a.priority]

        if banda >= piso:
            escala = banda > banda_actual
            reemision = (
                banda == banda_actual and banda > 0 and ultimo_T is not None
                and a.T - ultimo_T >= ev.refractario
                and a.risk - ultimo_risk >= ev.delta_reemision
            )
            if escala or reemision:
                salida.append(a)
                ultimo_T, ultimo_risk = a.T, a.risk
            banda_actual = max(banda_actual, banda)
            continue

        # Demotada por supresión. El criterio no es un umbral arbitrario sino si
        # la supresión efectivamente le costó una banda: esas señales son la
        # demostración del control de falsas alertas y por eso se emiten.
        if a.supresiones and a.bruto > 0:
            risk_bruto = 1.0 - math.exp(-a.bruto / float(cfg.puntaje["k0"]))
            banda_sin_supresion = _banda_por_riesgo(risk_bruto, ev)
            if banda_sin_supresion > banda and banda_sin_supresion >= piso and (
                    ultimo_T is None or a.T - ultimo_T >= ev.refractario):
                salida.append(a)
                ultimo_T, ultimo_risk = a.T, a.risk

        banda_actual = banda

    return salida


def barrer(store, cfg: ScoringConfig, ev: EventConfig, *,
           pacientes: list[str] | None = None,
           progreso: Callable[[int, int, int], None] | None = None
           ) -> tuple[list[Assessment], Stats]:
    """Recorre la cohorte paciente por paciente y devuelve las señales emitidas."""
    pacientes = pacientes if pacientes is not None else store.patients()
    stats = Stats()
    señales: list[Assessment] = []

    for i, pid in enumerate(pacientes, start=1):
        dictamenes = []
        for a in evaluar_paciente(store, pid, cfg, ev):
            stats.registrar(a)
            dictamenes.append(a)
        señales.extend(eventizar(dictamenes, cfg, ev))
        if progreso and (i % 50 == 0 or i == len(pacientes)):
            progreso(i, len(pacientes), len(señales))

    return señales, stats


def _banda_por_riesgo(risk: float, ev: EventConfig) -> int:
    """Banda que correspondería sólo por puntaje, ignorando compuertas duras."""
    if risk >= ev.umbral_critical:
        return ORDEN["CRITICAL"]
    if risk >= ev.umbral_high:
        return ORDEN["HIGH"]
    if risk >= ev.umbral_medium:
        return ORDEN["MEDIUM"]
    return ORDEN["LOW"]


def signal_id(a: Assessment) -> str:
    """Determinista y estable entre corridas: misma entrada, mismo identificador."""
    return f"HS-{a.patient_id.replace('PAT-', '')}-{a.T.strftime('%Y%m%dT%H%M')}"
