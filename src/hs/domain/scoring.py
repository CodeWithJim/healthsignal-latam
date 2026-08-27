"""El analizador. Función pura sobre un snapshot: sin IO, sin base, sin reloj.

Detecta **concordancia**: varios canales moviéndose a la vez en la dirección
clínicamente coherente y de forma sostenida, medidos contra el propio historial
del paciente. Un valor extremo aislado no alcanza (P-07).

La evidencia se recolecta desde los mismos objetos que produjeron el puntaje;
no se reconstruye después (P-03).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .models import Interval, PatientSnapshot, Series

# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ScoringConfig:
    """Constantes de calibración. Vienen de config/scoring.yaml (RNF-05)."""

    evidencia_h: float
    baseline_h: float
    nominal_min: Mapping[str, float]
    canales: Mapping[str, Mapping[str, float]]
    piso: Mapping[str, float]
    minimos: Mapping[str, float]
    puntaje: Mapping[str, Any]
    multifuente: Mapping[str, float]
    supresion: Mapping[str, Mapping[str, float]]
    prioridad: Mapping[str, Mapping[str, float]]
    confianza: Mapping[str, float]
    model_version: str

    @classmethod
    def from_dict(cls, d: dict) -> "ScoringConfig":
        return cls(
            evidencia_h=float(d["ventanas"]["evidencia_horas"]),
            baseline_h=float(d["ventanas"]["baseline_horas"]),
            nominal_min=d["muestreo_nominal_min"],
            canales=d["canales"],
            piso=d["piso_dispersion"],
            minimos=d["minimos"],
            puntaje=d["puntaje"],
            multifuente=d["multifuente"],
            supresion=d["supresion"],
            prioridad=d["prioridad"],
            confianza=d["confianza"],
            model_version=d["model_version"],
        )

    @property
    def evidencia(self) -> dt.timedelta:
        return dt.timedelta(hours=self.evidencia_h)

    @property
    def baseline(self) -> dt.timedelta:
        return dt.timedelta(hours=self.baseline_h)


# --------------------------------------------------------------------------- resultados


@dataclass(frozen=True)
class Citation:
    source_file: str
    record_id: str
    variable_code: str | None
    event_time: dt.datetime
    available_time: dt.datetime
    role: str                    # PRIMARY | SUPPORTING | CONTEXT | QUALITY
    contribution: float | None = None


@dataclass(frozen=True)
class ChannelScore:
    variable_code: str
    n_evidencia: int
    n_baseline: int
    mediana_baseline: float
    escala: float
    nivel: float                 # desviación del último tercio, en escalas
    pendiente: float             # deriva total de la ventana, en escalas
    persistencia: float          # fracción de la ventana del lado del deterioro
    s: float                     # contribución final, recortada a [0, techo]
    cobertura: float
    ultimo_valor: float
    muestras: tuple[tuple[str, str, dt.datetime, dt.datetime], ...] = ()


@dataclass(frozen=True)
class Suppression:
    regla: str
    fuerza: float
    motivo: str
    citas: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class Assessment:
    patient_id: str
    T: dt.datetime
    evidence_start: dt.datetime
    evidence_end: dt.datetime
    canales: Mapping[str, ChannelScore]
    k: int
    bruto: float                 # S antes de supresión
    puntaje: float               # S'
    risk: float
    confidence: float
    priority: str
    multifuente: float
    supresiones: tuple[Suppression, ...]
    citas: tuple[Citation, ...]
    explicacion: str
    model_version: str
    notas: tuple[str, ...] = field(default_factory=tuple)

    @property
    def concordantes(self) -> tuple[str, ...]:
        return tuple(c.variable_code
                     for c in sorted(self.canales.values(), key=lambda c: -c.s) if c.s > 0)

    def dominante(self) -> ChannelScore | None:
        vivos = [c for c in self.canales.values() if c.s > 0]
        return max(vivos, key=lambda c: c.s) if vivos else None


# --------------------------------------------------------------------------- estadística


def theil_sen(x: np.ndarray, y: np.ndarray) -> float:
    """Mediana de las pendientes entre todos los pares de puntos.

    Un valor atípico no la mueve, a diferencia de mínimos cuadrados: es lo que
    impide que un artefacto de un solo punto fabrique una tendencia inexistente.
    """
    n = x.size
    if n < 2:
        return 0.0
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    ok = dx != 0
    if not ok.any():
        return 0.0
    return float(np.median((y[j][ok] - y[i][ok]) / dx[ok]))


def mad_scale(v: np.ndarray) -> float:
    """Desviación robusta comparable a sigma."""
    if v.size == 0:
        return 0.0
    return float(np.median(np.abs(v - np.median(v))) * 1.4826)


def racha_maxima(mask: np.ndarray) -> int:
    """Muestras consecutivas verdaderas más largas."""
    mejor = actual = 0
    for x in mask:
        actual = actual + 1 if x else 0
        mejor = max(mejor, actual)
    return mejor


def persistencia(z: np.ndarray, umbral: float, objetivo: int) -> float:
    """¿La desviación se sostiene, o es un pico aislado?

    Se mide como la racha consecutiva más larga, no como la fracción de la
    ventana. La diferencia importa: una señal temprana ocupa poco de una ventana
    de 6 h y la fracción la castigaría justo cuando más vale detectarla, mientras
    que un artefacto de una sola muestra queda en racha 1 con cualquiera de las
    dos definiciones.
    """
    if z.size == 0 or objetivo <= 0:
        return 0.0
    return float(min(1.0, racha_maxima(z > umbral) / objetivo))


# --------------------------------------------------------------------------- por canal


def score_channel(code: str, evidencia: Series, base: Series,
                  t0: dt.datetime, t1: dt.datetime, cfg: ScoringConfig
                  ) -> ChannelScore | None:
    """Tres medidas del canal, o None si no alcanza para emitir juicio (RF-05)."""
    meta = cfg.canales.get(code)
    if meta is None:
        return None
    nominal = float(cfg.nominal_min.get(code, 20))
    direccion = float(meta["direccion"])

    esperadas_bl = (cfg.baseline_h * 60) / nominal
    if base.values.size < max(3, esperadas_bl * float(cfg.minimos["cobertura_baseline"])):
        return None

    cobertura = evidencia.coverage(t0, t1, nominal)
    esperadas_ev = max(1.0, (cfg.evidencia_h * 60) / nominal)
    minimo_ev = max(float(cfg.minimos["muestras_evidencia"]),
                    esperadas_ev * float(cfg.minimos["cobertura_evidencia"]))
    if evidencia.values.size < min(minimo_ev, esperadas_ev):
        return None

    m = float(np.median(base.values))
    s = max(mad_scale(base.values), float(cfg.piso.get(code, 0.1)))

    z = (evidencia.values - m) / s * direccion            # >0 = hacia el deterioro
    tercio = max(1, z.size // 3)
    nivel = float(np.median(z[-tercio:]))

    if z.size >= int(cfg.minimos["muestras_para_pendiente"]):
        horas = (evidencia.times - evidencia.times[0]) / np.timedelta64(1, "h")
        pendiente = theil_sen(horas.astype(float), z) * cfg.evidencia_h
    else:
        pendiente = 0.0                                    # muy pocos puntos: sólo nivel

    # La racha objetivo se expresa en tiempo y se traduce a muestras con la
    # grilla del canal: 1 h son 3 muestras de HR pero sólo 1 de TEMP. Medirla en
    # muestras haría que sostener una fiebre 3 h costara lo mismo que sostener
    # una taquicardia 1 h.
    objetivo = max(2, math.ceil(float(cfg.puntaje["racha_objetivo_min"]) / nominal))
    pers = persistencia(z, float(cfg.puntaje["umbral_persistencia"]), objetivo)

    pn, pp = float(cfg.puntaje["peso_nivel"]), float(cfg.puntaje["peso_pendiente"])
    if pendiente == 0.0 and z.size < int(cfg.minimos["muestras_para_pendiente"]):
        crudo = nivel                                      # todo el peso al nivel
    else:
        crudo = pn * nivel + pp * pendiente
    s_final = float(np.clip(crudo * pers, 0.0, float(cfg.puntaje["techo_por_canal"])))

    muestras = tuple(
        (evidencia.source_files[i], evidencia.record_ids[i],
         evidencia.times[i].astype(dt.datetime), evidencia.available[i].astype(dt.datetime))
        for i in _indices_representativos(z)
    )
    return ChannelScore(code, int(z.size), int(base.values.size), m, s,
                        nivel, pendiente, pers, s_final, cobertura,
                        float(evidencia.values[-1]), muestras)


def _indices_representativos(z: np.ndarray) -> list[int]:
    """Primera, última y la más extrema: bastan para reconstruir la trayectoria."""
    if z.size == 0:
        return []
    idx = {0, z.size - 1, int(np.argmax(z))}
    if z.size >= 6:
        idx.add(z.size // 2)
    return sorted(idx)


# --------------------------------------------------------------------------- supresión


def _sup_actividad(snap, canales, t0, t1, cfg) -> Suppression | None:
    p = cfg.supresion["actividad"]
    ivs = snap.intervals_overlapping(
        t0, t1, kind="CONTEXT",
        subtypes=("PHYSICAL_ACTIVITY", "RECOVERY_PHASE"),
    )
    ivs = [iv for iv in ivs
           if iv.subtype == "RECOVERY_PHASE" or iv.value_text in ("HIGH", "MODERATE")]
    if not ivs:
        return None
    total = sum(c.s for c in canales.values() if c.s > 0)
    hr = canales.get("HR")
    if not hr or total <= 0 or hr.s / total < float(p["dominancia_hr"]):
        return None
    citas = tuple(Citation(iv.source_file, iv.record_id, None, iv.start,
                           iv.available, "CONTEXT") for iv in ivs)
    etiquetas = ", ".join(f"{iv.subtype}={iv.value_text}" for iv in ivs[:3])
    return Suppression("actividad", float(p["fuerza"]),
                       f"desviación dominada por HR ({hr.s / total:.0%} del puntaje) "
                       f"con contexto de actividad disponible ({etiquetas})", citas)


def _sup_calidad(snap, canales, t0, t1, cfg) -> Suppression | None:
    p = cfg.supresion["calidad"]
    citas: list[Citation] = []
    malas = buenas = 0
    for code in canales:
        s = snap.window(code, t0, t1)
        mal = s.implausible()
        malas += len(mal)
        buenas += len(s.usable())
        for i in range(len(mal)):
            citas.append(Citation(mal.source_files[i], mal.record_ids[i], code,
                                  mal.times[i].astype(dt.datetime),
                                  mal.available[i].astype(dt.datetime), "QUALITY"))
    total = malas + buenas
    if total == 0 or malas / total < float(p["fraccion"]):
        return None
    return Suppression("calidad", float(p["fuerza"]),
                       f"{malas} de {total} muestras de la ventana están fuera de "
                       f"rango de plausibilidad", tuple(citas[:8]))


def _sup_transitorio(snap, canales, t0, t1, cfg) -> Suppression | None:
    p = cfg.supresion["transitorio"]
    vivos = [c for c in canales.values() if c.s > 0]
    if not vivos:
        return None
    pmax = max(c.persistencia for c in vivos)
    if pmax >= float(p["persistencia_min"]):
        return None
    dom = max(vivos, key=lambda c: c.s)
    citas = tuple(Citation(sf, rid, dom.variable_code, et, av, "QUALITY")
                  for sf, rid, et, av in dom.muestras)
    return Suppression("transitorio", float(p["fuerza"]),
                       f"la desviación no persiste: sólo {pmax:.0%} de la ventana "
                       f"se mantiene del lado del deterioro", citas)


def _sup_cobertura(snap, canales, t0, t1, cfg) -> Suppression | None:
    p = cfg.supresion["cobertura"]
    vivos = [c for c in canales.values() if c.s > 0] or list(canales.values())
    if not vivos:
        return None
    cob = float(np.mean([c.cobertura for c in vivos]))
    if cob >= float(p["umbral"]):
        return None
    ivs = snap.intervals_overlapping(t0, t1, kind="CONNECTIVITY")
    citas = tuple(Citation(iv.source_file, iv.record_id, None, iv.start,
                           iv.available, "QUALITY") for iv in ivs)
    return Suppression("cobertura", 0.0,
                       f"cobertura de la ventana {cob:.0%}: la señal se emite con "
                       f"confianza reducida y prioridad acotada", citas)


REGLAS = (_sup_actividad, _sup_calidad, _sup_transitorio, _sup_cobertura)


# --------------------------------------------------------------------------- multifuente


def _multifuente(snap: PatientSnapshot, t1: dt.datetime,
                 cfg: ScoringConfig) -> tuple[float, tuple[Citation, ...]]:
    """Laboratorios disponibles en T y fuera de su rango de referencia."""
    desde = t1 - dt.timedelta(hours=float(cfg.multifuente["ventana_lab_horas"]))
    aportes, citas = 0, []
    for code in ("LAB_A", "LAB_B", "LAB_C", "LAB_D"):
        s = snap.channel(code).usable().slice(desde, t1)
        if not len(s):
            continue
        i = int(np.argmax(s.times))
        citas.append(Citation(s.source_files[i], s.record_ids[i], code,
                              s.times[i].astype(dt.datetime),
                              s.available[i].astype(dt.datetime), "SUPPORTING"))
        aportes += 1
    aportes = min(aportes, int(cfg.multifuente["max_aportes"]))
    return float(cfg.multifuente["lambda"]) * aportes, tuple(citas)


# --------------------------------------------------------------------------- dictamen


def assess(snap: PatientSnapshot, cfg: ScoringConfig) -> Assessment:
    """Dictamen del paciente en el instante del snapshot."""
    T = snap.T
    t1, t0 = T, T - cfg.evidencia
    b1, b0 = t0, t0 - cfg.baseline

    canales: dict[str, ChannelScore] = {}
    for code in cfg.canales:
        ev = snap.window(code, t0, t1).usable()
        bl = snap.window(code, b0, b1).usable()
        cs = score_channel(code, ev, bl, t0, t1, cfg)
        if cs is not None:
            canales[code] = cs

    umbral = float(cfg.puntaje["umbral_canal_concordante"])
    k = sum(1 for c in canales.values() if c.s >= umbral)
    conc = cfg.puntaje["concordancia"]
    mult = float(conc.get(k, conc.get(str(k), 1.4 if k >= 4 else 0.0)))

    base = sum(float(cfg.canales[c.variable_code]["peso"]) * c.s
               for c in canales.values() if c.s > 0)
    ms, citas_ms = _multifuente(snap, t1, cfg)
    bruto = base * mult + (ms if k >= 1 else 0.0)

    supresiones = tuple(r for r in (f(snap, canales, t0, t1, cfg) for f in REGLAS) if r)
    puntaje = bruto
    for sup in supresiones:
        puntaje *= (1.0 - sup.fuerza)

    risk = 1.0 - math.exp(-puntaje / float(cfg.puntaje["k0"])) if puntaje > 0 else 0.0
    confidence = _confianza(snap, canales, t0, t1, cfg)
    priority = _prioridad(risk, k, canales, confidence, supresiones, cfg)

    citas = _citas(snap, canales, supresiones, citas_ms, t0, t1, k)
    expl = _explicacion(canales, k, risk, priority, supresiones, ms, confidence, cfg)

    return Assessment(snap.patient_id, T, t0, t1, canales, k, bruto, puntaje,
                      risk, confidence, priority, ms, supresiones, citas, expl,
                      cfg.model_version)


def _confianza(snap, canales, t0, t1, cfg) -> float:
    """Cobertura x calidad x frescura. Independiente del riesgo (RF-09, P-06)."""
    if not canales:
        return 0.0
    cob = float(np.mean([c.cobertura for c in canales.values()]))
    malas = buenas = 0
    for code in canales:
        s = snap.window(code, t0, t1)
        malas += len(s.implausible())
        buenas += len(s.usable())
    calidad = 1.0 if (malas + buenas) == 0 else buenas / (malas + buenas)
    edades = []
    for code in canales:
        s = snap.window(code, t0, t1).usable()
        if len(s):
            edades.append((t1 - s.available.max().astype(dt.datetime)).total_seconds() / 60)
    tau = float(cfg.confianza["tau_frescura_min"])
    frescura = 1.0 if not edades else float(np.clip(1.0 - min(edades) / tau, 0.3, 1.0))
    return float(np.clip(cob * calidad * frescura, 0.0, 1.0))


def _prioridad(risk, k, canales, confidence, supresiones, cfg) -> str:
    """Compuertas duras: un solo canal jamás llega a CRITICAL (P-07)."""
    activas = [s for s in supresiones if s.fuerza > 0]
    tope_cobertura = any(s.regla == "cobertura" for s in supresiones)
    vivos = [c for c in canales.values() if c.s > 0]
    pmax = max((c.persistencia for c in vivos), default=0.0)
    cob = float(np.mean([c.cobertura for c in canales.values()])) if canales else 0.0

    c = cfg.prioridad["CRITICAL"]
    if (risk >= float(c["risk"]) and k >= int(c["k"]) and pmax >= float(c["persistencia"])
            and cob >= float(c["cobertura"]) and not activas and not tope_cobertura):
        return "CRITICAL"
    h = cfg.prioridad["HIGH"]
    if risk >= float(h["risk"]) and k >= int(h["k"]):
        return "HIGH"
    if risk >= float(cfg.prioridad["MEDIUM"]["risk"]):
        return "MEDIUM"
    return "LOW"


def _citas(snap, canales, supresiones, citas_ms, t0, t1, k) -> tuple[Citation, ...]:
    """La evidencia sale del mismo cómputo que produjo el puntaje (P-03)."""
    vivos = sorted((c for c in canales.values() if c.s > 0), key=lambda c: -c.s)
    dom = vivos[0].variable_code if vivos else None
    out: list[Citation] = []
    for c in vivos:
        rol = "PRIMARY" if c.variable_code == dom else "SUPPORTING"
        for sf, rid, et, av in c.muestras:
            out.append(Citation(sf, rid, c.variable_code, et, av, rol, round(c.s, 4)))
    out.extend(citas_ms)
    for sup in supresiones:
        out.extend(sup.citas)
    for x in snap.excluded:
        if t0 <= x.event_time <= t1:
            out.append(Citation(x.source_file, x.record_id, x.variable_code,
                                x.event_time, x.available_time, "QUALITY"))
    e = snap.encounter()
    if e and not any(c.role == "CONTEXT" for c in out):
        out.append(Citation(e.source_file, e.record_id, None, e.start, e.available, "CONTEXT"))
    vistos, unicas = set(), []
    for c in out:
        llave = (c.source_file, c.record_id, c.role)
        if llave not in vistos:
            vistos.add(llave)
            unicas.append(c)
    return tuple(unicas[:32])


def _explicacion(canales, k, risk, priority, supresiones, ms, confidence, cfg) -> str:
    """Determinista, verificable contra la evidencia, sin afirmación clínica (P-01)."""
    vivos = sorted((c for c in canales.values() if c.s > 0), key=lambda c: -c.s)
    if not vivos:
        return (f"Sin desviación concordante en la ventana de {cfg.evidencia_h:.0f} h "
                f"sobre {len(canales)} canal(es) evaluado(s).")
    detalle = "; ".join(
        f"{c.variable_code} {c.nivel:+.1f}s nivel / {c.pendiente:+.1f}s deriva, "
        f"persistencia {c.persistencia:.0%}" for c in vivos[:4])
    partes = [f"{k}/{len(canales)} canales concordantes en {cfg.evidencia_h:.0f} h: {detalle}"]
    if ms > 0:
        partes.append(f"corroboración de laboratorio disponible ({ms:.0f} marcador/es)")
    for s in supresiones:
        verbo = "prioridad acotada" if s.fuerza == 0 else f"puntaje reducido {s.fuerza:.0%}"
        partes.append(f"{verbo} por {s.regla}: {s.motivo}")
    partes.append(f"riesgo {risk:.2f}, confianza {confidence:.2f}, prioridad {priority}")
    return ". ".join(partes) + "."
