"""Capa de narración fundamentada, posterior a la decisión.

OpenAI no recibe datos crudos ni participa en el cálculo de riesgo. Recibe un
paquete estructurado derivado de ``Assessment`` y sólo redacta el título, el
resumen y los puntos de revisión. Los valores reales y la procedencia que se
muestran en la interfaz se construyen localmente y no pueden ser modificados
por el modelo generativo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from .. import paths
from ..detect.history import HistoryAnalysis
from ..domain.scoring import Assessment

load_dotenv(paths.ROOT / ".env", override=False)

DEFAULT_MODEL = "gpt-5.6-terra"
DISCLAIMER = (
    "Apoyo para revisión profesional. No constituye diagnóstico, prescripción "
    "ni decisión clínica autónoma."
)

LABELS = {
    "HR": "frecuencia cardíaca",
    "RR": "frecuencia respiratoria",
    "SpO2": "saturación de oxígeno",
    "TEMP": "temperatura",
    "SBP": "presión arterial sistólica",
    "DBP": "presión arterial diastólica",
}

UNITS = {
    "HR": "bpm",
    "RR": "rpm",
    "SpO2": "%",
    "TEMP": "°C",
    "SBP": "mmHg",
    "DBP": "mmHg",
}

DEFAULT_PROMPT = """
Actúas como redactor de apoyo a la revisión profesional de una cohorte clínica
sintética. No eres el motor de decisión y no actúas como médico. Tu única fuente
es el Assessment puntual o el resumen longitudinal estructurado que entrega la
aplicación.

Objetivo:
- Explica primero, en lenguaje humano y sin rodeos, qué patrón muestran los datos.
- Indica qué variables cambian de forma concordante o sostenida y por qué el
  patrón fue priorizado, usando sólo las relaciones presentes en el Assessment.
- Propón dos o tres aspectos concretos que el profesional debería revisar:
  evolución conjunta, consistencia de las mediciones, valoración actual,
  contexto registrado o corroboración disponible.
- Si la evidencia es insuficiente o el cambio es aislado, dilo de forma explícita.
- En un análisis longitudinal, distingue siempre el estado al cierre de la
  máxima prioridad observada; un episodio pasado no describe automáticamente
  el estado final del paciente.

Reglas obligatorias:
- No diagnostiques, pronostiques, prescribas ni sugieras tratamientos, dosis,
  medicamentos o decisiones clínicas.
- No atribuyas causas, síntomas, enfermedades ni consecuencias que no estén
  declaradas en los datos recibidos.
- No cambies ni reinterpretes el riesgo, la prioridad, la confianza, las reglas
  evaluadas o la evidencia seleccionada por el motor determinista.
- No incluyas cifras en el título, resumen o puntos de revisión. La aplicación
  mostrará por separado todos los valores exactos.
- Cita únicamente identificadores incluidos en evidence_refs_allowed.
- Separa hechos de interpretación: usa expresiones como "los datos muestran",
  "amerita revisar" o "conviene contrastar".

Estilo:
- Español claro, sobrio y breve.
- Título específico; resumen de dos a cuatro oraciones; puntos accionables para
  revisión, sin recomendaciones terapéuticas.
- Evita introducciones genéricas, repeticiones y lenguaje alarmista.
""".strip()

_FORBIDDEN = re.compile(
    r"\b(diagn[oó]stic|prescrib|tratamiento|medicamento|dosis|administrar|"
    r"suspender|enfermedad|padece)\w*\b",
    re.IGNORECASE,
)
# No bloquea codigos como SpO2, pero si numeros escritos como afirmaciones.
_NUMERIC_CLAIM = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?(?![A-Za-z])")


class NarrativeText(BaseModel):
    """Única parte del resultado que puede redactar OpenAI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=8, max_length=140)
    summary: str = Field(min_length=30, max_length=800)
    review_points: list[str] = Field(min_length=2, max_length=3)
    evidence_refs: list[str] = Field(max_length=6)


_cache: dict[tuple[str, str], NarrativeText] = {}
_cache_lock = threading.Lock()
log = logging.getLogger(__name__)


def _api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY")


def _model() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def narrative_status(use_openai: bool | None = None) -> dict:
    """Configuración pública, sin revelar la clave ni activar llamadas externas."""
    configured = bool(_api_key())
    enabled = configured if use_openai is None else configured and use_openai
    return {
        "enabled": enabled,
        "configured": configured,
        "provider": "openai" if enabled else "deterministic_fallback",
        "model": _model(),
    }


def _findings(a: Assessment) -> list[dict]:
    """Datos reales que la UI muestra sin pasar por generacion."""
    findings = []
    channels = sorted((c for c in a.canales.values() if c.s > 0), key=lambda c: -c.s)
    for c in channels[:5]:
        refs = list(dict.fromkeys(row[1] for row in c.muestras))
        findings.append({
            "variable_code": c.variable_code,
            "label": LABELS.get(c.variable_code, c.variable_code),
            "baseline": round(c.mediana_baseline, 2),
            "latest": round(c.ultimo_valor, 2),
            "unit": UNITS.get(c.variable_code, ""),
            "direction": "aumento" if c.ultimo_valor >= c.mediana_baseline else "descenso",
            "persistence": round(c.persistencia, 3),
            "coverage": round(c.cobertura, 3),
            "contribution": round(c.s, 3),
            "evidence_refs": refs,
        })
    return findings


def _allowed_refs(a: Assessment) -> list[str]:
    return list(dict.fromkeys(c.record_id for c in a.citas))


def _grounding(a: Assessment, findings: list[dict], allowed: list[str]) -> dict:
    """Paquete acotado: no incluye texto libre de archivos de origen."""
    return {
        "patient_id": a.patient_id,
        "decision_datetime": a.T.isoformat(),
        "evidence_window": {
            "start": a.evidence_start.isoformat(),
            "end": a.evidence_end.isoformat(),
        },
        "risk_score": round(a.risk, 4),
        "confidence_score": round(a.confidence, 4),
        "priority_level": a.priority,
        "concordant_channels": a.k,
        "findings": findings,
        "rules_evaluated": [
            {"rule": s.regla, "strength": s.fuerza, "reason": s.motivo}
            for s in a.supresiones
        ],
        "laboratory_corroboration": round(a.multifuente, 3),
        "evidence_refs_allowed": allowed,
    }


def _fallback_text(a: Assessment, findings: list[dict], allowed: list[str]) -> NarrativeText:
    priority_label = {
        "CRITICAL": "crítica",
        "HIGH": "alta",
        "MEDIUM": "media",
        "LOW": "baja",
    }.get(a.priority, a.priority.lower())
    selected = findings[:min(max(a.k, 1), 4)]
    names = [f["label"] for f in selected]
    if names and a.k >= 2:
        joined = ", ".join(names[:-1]) + (f" y {names[-1]}" if len(names) > 1 else names[0])
        channel_word = "canal" if a.k == 1 else "canales"
        title = f"Señal {priority_label}: patrón concordante que amerita revisión"
        summary = (
            f"Los datos muestran cambios sostenidos y concordantes en {joined}. "
            f"La prioridad se sustenta en {a.k} {channel_word} concordantes, junto con "
            "la persistencia, cobertura y contexto disponibles en la ventana analizada."
        )
        review = [
            f"Revisar conjuntamente la evolución de {joined} y contrastarla con la valoración actual.",
            "Confirmar la consistencia y calidad de las mediciones citadas antes de interpretar el patrón.",
        ]
    elif names:
        joined = names[0]
        title = f"Señal {priority_label}: cambio aislado para revisar"
        summary = (
            f"Los datos muestran un cambio sostenido en {joined}, pero no forman un patrón "
            "multivariable concordante suficiente en la ventana analizada."
        )
        review = [
            f"Revisar la evolución de {joined} y contrastarla con la valoración actual.",
            "Confirmar la consistencia y calidad de las mediciones antes de interpretar el cambio aislado.",
        ]
    else:
        title = "Sin patrón concordante prioritario en la ventana"
        summary = (
            "Los datos disponibles no forman una desviación multivariable sostenida "
            "con evidencia suficiente para priorizar este instante."
        )
        review = [
            "Revisar la cobertura y frescura de las mediciones disponibles.",
            "Mantener la lectura del resultado separada de cualquier conclusión diagnóstica.",
        ]

    if any(c.role == "SUPPORTING" and c.variable_code and c.variable_code.startswith("LAB_")
           for c in a.citas):
        review.append("Contrastar el patrón con los resultados de laboratorio disponibles y su momento de reporte.")
    elif a.supresiones:
        review.append("Contrastar el patrón con el contexto, la conectividad y las reglas de supresión registradas.")

    return NarrativeText(
        title=title,
        summary=summary,
        review_points=review[:3],
        evidence_refs=allowed[:6],
    )


def _validate_generated(text: NarrativeText, allowed: list[str]) -> NarrativeText:
    prose = " ".join([text.title, text.summary, *text.review_points])
    if _FORBIDDEN.search(prose):
        raise ValueError("la narrativa contiene lenguaje clinico no permitido")
    if _NUMERIC_CLAIM.search(prose):
        raise ValueError("la narrativa generativa contiene cifras; deben venir de findings")

    invalid = set(text.evidence_refs) - set(allowed)
    if invalid:
        raise ValueError("la narrativa cito evidencia fuera del Assessment")

    # El orden del modelo se conserva, eliminando duplicados. Si no eligio
    # referencias, se usan las primeras ya seleccionadas por el motor.
    refs = list(dict.fromkeys(text.evidence_refs)) or allowed[:6]
    return text.model_copy(update={"evidence_refs": refs[:6]})


def _history_grounding(h: HistoryAnalysis, findings: list[dict], allowed: list[str]) -> dict:
    return {
        "analysis_scope": "longitudinal_history",
        "history_window": {"start": h.start.isoformat(), "end": h.end.isoformat()},
        "evaluations_with_baseline": len(h.assessments),
        "evaluations_without_baseline": h.skipped_without_baseline,
        "priority_counts": dict(h.priority_counts),
        "priority_transitions": [
            {"at": t.at.isoformat(), "priority": t.priority, "risk_score": round(t.risk, 4)}
            for t in h.transitions
        ],
        "assessment_at_close": _grounding(h.current, _findings(h.current), _allowed_refs(h.current)),
        "maximum_priority_assessment": _grounding(h.peak, findings, allowed),
        "evidence_refs_allowed": allowed,
    }


def _history_fallback(h: HistoryAnalysis, findings: list[dict], allowed: list[str]) -> NarrativeText:
    labels = {
        "CRITICAL": "crítica",
        "HIGH": "alta",
        "MEDIUM": "media",
        "LOW": "baja",
    }
    peak_label = labels.get(h.peak.priority, h.peak.priority.lower())
    current_label = labels.get(h.current.priority, h.current.priority.lower())
    names = [f["label"] for f in findings[:3]]
    variables = ", ".join(names[:-1]) + (f" y {names[-1]}" if len(names) > 1 else (names[0] if names else ""))

    if h.peak.priority != h.current.priority:
        title = f"Historia con prioridad máxima {peak_label} y cierre {current_label}"
        summary = (
            f"En el período analizado se observó un episodio de prioridad {peak_label}. "
            f"Al cierre, el estado calculado fue de prioridad {current_label}; por eso el episodio "
            "histórico y el estado final deben interpretarse por separado."
        )
    else:
        title = f"Historia con prioridad máxima y cierre {current_label}"
        summary = (
            f"La máxima prioridad observada y el estado calculado al cierre fueron {current_label}. "
            "La conclusión resume todo el período sin usar información posterior a cada evaluación."
        )
    if variables:
        summary += f" El episodio máximo estuvo sustentado principalmente por {variables}."
    if h.current.multifuente and not h.peak.multifuente:
        summary += (
            " Al cierre también había resultados de laboratorio fuera de referencia, "
            "aunque no elevaron la máxima prioridad calculada."
        )

    review = [
        "Revisar el momento de máxima prioridad y compararlo con el estado al cierre.",
        "Contrastar la trayectoria completa de los canales y la calidad de sus mediciones.",
    ]
    if h.peak.multifuente or h.current.multifuente:
        review.append("Contrastar la trayectoria con los laboratorios y su momento de disponibilidad.")
    else:
        review.append("Revisar el contexto registrado alrededor de los cambios de prioridad.")
    return NarrativeText(
        title=title,
        summary=summary,
        review_points=review,
        evidence_refs=allowed[:6],
    )


def _call_openai(payload_json: str, api_key: str, model: str) -> NarrativeText:
    """Invocación aislada para poder sustituirla en pruebas."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=12.0, max_retries=0)
    try:
        response = client.responses.parse(
            model=model,
            instructions=DEFAULT_PROMPT,
            input=("Assessment estructurado. Usa exclusivamente estos datos:\n" + payload_json),
            text_format=NarrativeText,
            reasoning={"effort": "low"},
            max_output_tokens=1_500,
            store=False,
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI devolvió una respuesta vacía o no estructurada")
        return response.output_parsed
    finally:
        client.close()


def narrate(a: Assessment, *, use_openai: bool = True) -> dict:
    """Narrativa segura con fallback local y evidencia determinista."""
    findings = _findings(a)
    allowed = _allowed_refs(a)
    fallback = _fallback_text(a, findings, allowed)
    api_key, model = _api_key(), _model()

    source: Literal["openai", "deterministic_fallback"] = "deterministic_fallback"
    text = fallback
    if use_openai and api_key:
        payload_json = json.dumps(
            _grounding(a, findings, allowed), ensure_ascii=False, sort_keys=True,
        )
        cache_key = (model, payload_json)
        try:
            with _cache_lock:
                cached = _cache.get(cache_key)
            if cached is None:
                cached = _validate_generated(_call_openai(payload_json, api_key, model), allowed)
                with _cache_lock:
                    _cache[cache_key] = cached
            text = cached
            source = "openai"
        except Exception as exc:  # la decision debe sobrevivir a cualquier falla externa
            log.warning("Narrativa OpenAI no disponible; se usa fallback (%s)", type(exc).__name__)

    return {
        "scope": "point_in_time",
        "source": source,
        "model": model if source == "openai" else None,
        "title": text.title,
        "summary": text.summary,
        "review_points": text.review_points,
        "evidence_refs": text.evidence_refs,
        "findings": findings,
        "disclaimer": DISCLAIMER,
    }


def narrate_history(h: HistoryAnalysis, *, use_openai: bool = True) -> dict:
    """Narrativa del período completo, separando máximo histórico y cierre."""
    findings = _findings(h.peak)
    allowed = list(dict.fromkeys([*_allowed_refs(h.peak), *_allowed_refs(h.current)]))
    fallback = _history_fallback(h, findings, allowed)
    api_key, model = _api_key(), _model()

    source: Literal["openai", "deterministic_fallback"] = "deterministic_fallback"
    text = fallback
    if use_openai and api_key:
        payload_json = json.dumps(
            _history_grounding(h, findings, allowed), ensure_ascii=False, sort_keys=True,
        )
        cache_key = (model, payload_json)
        try:
            with _cache_lock:
                cached = _cache.get(cache_key)
            if cached is None:
                cached = _validate_generated(_call_openai(payload_json, api_key, model), allowed)
                with _cache_lock:
                    _cache[cache_key] = cached
            text = cached
            source = "openai"
        except Exception as exc:
            log.warning("Narrativa histórica OpenAI no disponible; se usa fallback (%s)",
                        type(exc).__name__)

    return {
        "scope": "longitudinal_history",
        "source": source,
        "model": model if source == "openai" else None,
        "title": text.title,
        "summary": text.summary,
        "review_points": text.review_points,
        "evidence_refs": text.evidence_refs,
        "findings": findings,
        "disclaimer": DISCLAIMER,
    }
