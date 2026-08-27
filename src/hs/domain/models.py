"""Objetos de dominio. Puros: sin IO, sin base de datos, sin reloj.

Todo lo que el motor de decisión conoce entra por aquí. Las series guardan los
valores en arreglos vectorizados y, en paralelo, el `record_id` y el
`source_file` de cada muestra: la evidencia se produce desde el mismo cómputo
que produce el puntaje, nunca se reconstruye después (P-03).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import numpy as np

DT64 = "datetime64[us]"


def _as64(t: dt.datetime | np.datetime64) -> np.datetime64:
    return t if isinstance(t, np.datetime64) else np.datetime64(t, "us")


# --------------------------------------------------------------------------- series

@dataclass(frozen=True)
class Series:
    """Serie temporal de una variable, con procedencia por muestra.

    `times` y `available` están ordenados por tiempo de evento. Los cinco
    arreglos tienen el mismo largo y el mismo índice: la muestra i tiene valor
    `values[i]` y proviene de `source_files[i]` / `record_ids[i]`.
    """

    variable_code: str
    times: np.ndarray                    # datetime64[us] — cuándo ocurrió
    available: np.ndarray                # datetime64[us] — cuándo se pudo saber
    values: np.ndarray                   # float64, NaN si el valor es categórico
    texts: tuple[str | None, ...]
    record_ids: tuple[str, ...]
    source_files: tuple[str, ...]
    plausible: np.ndarray                # bool — gate propio (RD-06)
    unit: str | None = None
    # Rango de referencia por muestra. Sólo los laboratorios lo traen; en el
    # resto es NaN. Sin esto no se puede distinguir un resultado fuera de rango
    # de uno normal, y la corroboración multifuente pierde sentido.
    ref_low: np.ndarray | None = None
    ref_high: np.ndarray | None = None

    def __len__(self) -> int:
        return int(self.times.size)

    def __bool__(self) -> bool:
        return self.times.size > 0

    def _take(self, mask: np.ndarray) -> "Series":
        idx = np.flatnonzero(mask)
        return Series(
            self.variable_code, self.times[idx], self.available[idx], self.values[idx],
            tuple(self.texts[i] for i in idx), tuple(self.record_ids[i] for i in idx),
            tuple(self.source_files[i] for i in idx), self.plausible[idx], self.unit,
            None if self.ref_low is None else self.ref_low[idx],
            None if self.ref_high is None else self.ref_high[idx],
        )

    def fuera_de_rango(self) -> np.ndarray:
        """Máscara de muestras fuera de su rango de referencia declarado."""
        if self.ref_low is None or self.ref_high is None or not len(self):
            return np.zeros(len(self), dtype=bool)
        con_rango = ~np.isnan(self.ref_low) & ~np.isnan(self.ref_high)
        return con_rango & ((self.values < self.ref_low) | (self.values > self.ref_high))

    def contiguo(self, lo: int, hi: int) -> "Series":
        """Rebanada contigua [lo, hi). O(k) en vez de O(n): las tuplas se cortan."""
        return Series(
            self.variable_code, self.times[lo:hi], self.available[lo:hi],
            self.values[lo:hi], self.texts[lo:hi], self.record_ids[lo:hi],
            self.source_files[lo:hi], self.plausible[lo:hi], self.unit,
            None if self.ref_low is None else self.ref_low[lo:hi],
            None if self.ref_high is None else self.ref_high[lo:hi],
        )

    def rango_por_evento(self, t0: dt.datetime, t1: dt.datetime) -> "Series":
        """Recorte por tiempo de evento aprovechando que `times` está ordenado.

        Con 144.000 evaluaciones, una máscara booleana sobre la serie completa
        domina el costo; searchsorted la reduce a la porción que interesa.
        """
        lo = int(np.searchsorted(self.times, _as64(t0), side="left"))
        hi = int(np.searchsorted(self.times, _as64(t1), side="right"))
        return self.contiguo(lo, hi)

    def slice(self, t0: dt.datetime, t1: dt.datetime, *, inclusive: bool = True) -> "Series":
        """Recorta por tiempo de evento. `inclusive` incluye ambos extremos."""
        if inclusive:
            return self.rango_por_evento(t0, t1)
        a, b = _as64(t0), _as64(t1)
        return self._take((self.times > a) & (self.times <= b))

    def usable(self) -> "Series":
        """Sólo muestras plausibles y numéricas: lo que puede entrar a un cálculo."""
        return self._take(self.plausible & ~np.isnan(self.values))

    def categorical(self) -> "Series":
        """Muestras categóricas válidas: ACTIVITY_LEVEL, SLEEP_STATE.

        No tienen valor numérico, así que `usable()` las deja fuera por diseño;
        las reglas de contexto las necesitan por su texto.
        """
        con_texto = np.array([bool(t) for t in self.texts], dtype=bool)
        return self._take(self.plausible & con_texto)

    @property
    def is_categorical(self) -> bool:
        return bool(len(self)) and bool(np.isnan(self.values).all())

    def present(self) -> "Series":
        """Muestras utilizables, sea el canal numérico o categórico."""
        return self.categorical() if self.is_categorical else self.usable()

    def implausible(self) -> "Series":
        """Muestras descartadas del cálculo. Siguen siendo citables como QUALITY."""
        return self._take(~self.plausible)

    def last(self) -> tuple[dt.datetime, float, str] | None:
        if not len(self):
            return None
        i = int(np.argmax(self.times))
        return (self.times[i].astype(dt.datetime), float(self.values[i]), self.record_ids[i])

    def max_available(self) -> np.datetime64 | None:
        return None if not len(self) else self.available.max()

    def coverage(self, t0: dt.datetime, t1: dt.datetime, nominal_min: float) -> float:
        """Fracción de las muestras esperadas que efectivamente están presentes.

        Es un conteo exacto, no una estimación: la grilla nominal es regular y
        está medida (RD-08).
        """
        if nominal_min <= 0:
            return 1.0
        span_min = (t1 - t0).total_seconds() / 60.0
        expected = span_min / nominal_min
        if expected <= 0:
            return 1.0
        return float(min(1.0, len(self.slice(t0, t1)) / expected))

    @staticmethod
    def empty(variable_code: str) -> "Series":
        z = np.array([], dtype=DT64)
        return Series(variable_code, z, z.copy(), np.array([], dtype=float),
                      (), (), (), np.array([], dtype=bool), None)


# --------------------------------------------------------------------------- intervalos

@dataclass(frozen=True)
class Interval:
    """Rango con estado: contexto, conectividad, medicación o encuentro."""

    source_file: str
    record_id: str
    kind: str
    subtype: str | None
    value_text: str | None
    start: dt.datetime
    end: dt.datetime
    available: dt.datetime
    confidence: float | None = None
    extra: str | None = None

    def end_as_of(self, T: dt.datetime) -> dt.datetime:
        """Fin conocido en T.

        Un intervalo en curso todavía no tiene fin observado: saber que el sueño
        de un paciente termina a las 06:00 cuando son las 01:00 es información
        futura. Se recorta en T (P-02).
        """
        return min(self.end, T)

    def ongoing_at(self, T: dt.datetime) -> bool:
        return self.start <= T < self.end

    def overlaps(self, t0: dt.datetime, t1: dt.datetime) -> bool:
        return self.start <= t1 and self.end >= t0

    def contains(self, t: dt.datetime) -> bool:
        return self.start <= t <= self.end


@dataclass(frozen=True)
class ClinicalFact:
    """Antecedente registrado. Contexto basal, nunca diagnóstico de evento (P-01)."""

    source_file: str
    record_id: str
    category: str | None
    onset: dt.date | None
    available: dt.datetime
    status: str | None = None
    severity: str | None = None


@dataclass(frozen=True)
class ExcludedRow:
    """Fila apartada del cálculo que conserva su procedencia para citarla."""

    source_file: str
    record_id: str
    variable_code: str | None
    event_time: dt.datetime
    available_time: dt.datetime
    reason: str          # DUPLICATE | IMPLAUSIBLE


# --------------------------------------------------------------------------- snapshot

class CausalityError(AssertionError):
    """El snapshot contiene información que no estaba disponible en T."""


@dataclass(frozen=True)
class PatientSnapshot:
    """Todo lo que se sabía de un paciente en el instante T. Nada más.

    Es la única forma en que el motor de decisión ve datos. Su constructor
    verifica la invariante de P-02, de modo que un snapshot mal construido falla
    al crearse y no al exportar.
    """

    patient_id: str
    T: dt.datetime
    series: Mapping[str, Series]
    intervals: tuple[Interval, ...] = ()
    facts: tuple[ClinicalFact, ...] = ()
    excluded: tuple[ExcludedRow, ...] = ()
    lookback: dt.timedelta = field(default_factory=lambda: dt.timedelta(hours=54))

    def __post_init__(self) -> None:
        self.assert_causal()

    # ---- invariante

    def assert_causal(self) -> None:
        t = _as64(self.T)
        for code, s in self.series.items():
            mx = s.max_available()
            if mx is not None and mx > t:
                raise CausalityError(
                    f"{self.patient_id}: la serie {code} contiene una muestra disponible "
                    f"en {mx}, posterior a la decisión en {self.T}"
                )
        for iv in self.intervals:
            if iv.available > self.T:
                raise CausalityError(
                    f"{self.patient_id}: el intervalo {iv.record_id} pasó a estar "
                    f"disponible en {iv.available}, posterior a {self.T}"
                )
        for f in self.facts:
            if f.available > self.T:
                raise CausalityError(
                    f"{self.patient_id}: el antecedente {f.record_id} quedó registrado "
                    f"en {f.available}, posterior a {self.T}"
                )

    # ---- acceso

    def channel(self, code: str) -> Series:
        return self.series.get(code) or Series.empty(code)

    def window(self, code: str, t0: dt.datetime, t1: dt.datetime) -> Series:
        return self.channel(code).slice(t0, t1)

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self.series))

    def intervals_overlapping(
        self, t0: dt.datetime, t1: dt.datetime, *,
        kind: str | None = None, subtypes: Iterable[str] | None = None,
        values: Iterable[str] | None = None,
    ) -> tuple[Interval, ...]:
        st = set(subtypes) if subtypes else None
        vl = set(values) if values else None
        return tuple(
            iv for iv in self.intervals
            if iv.overlaps(t0, t1)
            and (kind is None or iv.kind == kind)
            and (st is None or iv.subtype in st)
            and (vl is None or iv.value_text in vl)
        )

    def encounter(self) -> Interval | None:
        for iv in self.intervals:
            if iv.kind == "ENCOUNTER":
                return iv
        return None

    def within_encounter(self) -> bool:
        """RD-09: fuera del encuentro no hay dato ausente, no hay monitoreo."""
        e = self.encounter()
        return bool(e and e.start <= self.T <= e.end)

    def n_observations(self) -> int:
        return sum(len(s) for s in self.series.values())

    def __repr__(self) -> str:
        return (f"PatientSnapshot({self.patient_id} @ {self.T}, "
                f"{len(self.series)} canales, {self.n_observations()} muestras, "
                f"{len(self.intervals)} intervalos, {len(self.facts)} antecedentes)")
