"""Análisis longitudinal de un paciente dentro de un intervalo.

Una decisión puntual responde "qué ocurría en T". Este módulo conserva esa
respuesta y, además, recorre causalmente el período para identificar el peor
episodio observado. No mezcla ambas cosas: un episodio CRITICAL pasado no
convierte automáticamente el estado al cierre en CRITICAL.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping

from ..domain.scoring import Assessment, ScoringConfig, assess
from .runner import ORDEN


@dataclass(frozen=True)
class PriorityTransition:
    at: dt.datetime
    priority: str
    risk: float


@dataclass(frozen=True)
class HistoryAnalysis:
    start: dt.datetime
    end: dt.datetime
    current: Assessment
    peak: Assessment
    assessments: tuple[Assessment, ...]
    priority_counts: Mapping[str, int]
    transitions: tuple[PriorityTransition, ...]
    skipped_without_baseline: int

    @property
    def first_evaluable_at(self) -> dt.datetime | None:
        return self.assessments[0].T if self.assessments else None


def analyze_history(timeline, start: dt.datetime, end: dt.datetime,
                    cfg: ScoringConfig, *, cadence: dt.timedelta = dt.timedelta(minutes=20)
                    ) -> HistoryAnalysis:
    """Evalúa el período completo sin usar información posterior a cada instante.

    La grilla de 20 minutos coincide con los canales más frecuentes del dataset.
    El instante final se agrega siempre, aunque no caiga en esa grilla, para que
    la respuesta al corte solicitado sea exacta.
    """
    if end < start:
        raise ValueError("el final del análisis es anterior a su inicio")
    if cadence.total_seconds() <= 0:
        raise ValueError("la cadencia debe ser positiva")

    lookback = cfg.evidencia + cfg.baseline
    moments: list[dt.datetime] = []
    t = start
    while t <= end:
        moments.append(t)
        t += cadence
    if not moments or moments[-1] != end:
        moments.append(end)

    all_assessments = tuple(assess(timeline.at(t, lookback), cfg) for t in moments)
    evaluable = tuple(a for a in all_assessments if a.canales)
    current = all_assessments[-1]
    candidates = evaluable or (current,)
    peak = max(candidates, key=lambda a: (ORDEN[a.priority], a.risk, a.T))

    counts = {priority: 0 for priority in ORDEN}
    transitions: list[PriorityTransition] = []
    previous: str | None = None
    for a in evaluable:
        counts[a.priority] += 1
        if a.priority != previous:
            transitions.append(PriorityTransition(a.T, a.priority, a.risk))
            previous = a.priority

    return HistoryAnalysis(
        start=start,
        end=end,
        current=current,
        peak=peak,
        assessments=evaluable,
        priority_counts=counts,
        transitions=tuple(transitions),
        skipped_without_baseline=len(all_assessments) - len(evaluable),
    )
