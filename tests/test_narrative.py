"""La IA redacta; nunca cambia la decision ni inventa evidencia."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import hs.narrative.service as narrative
from hs.detect.history import HistoryAnalysis, PriorityTransition
from hs.domain.scoring import assess
from trayectorias import caso_progresivo, config, snapshot


def _assessment():
    event = dt.datetime(2026, 7, 10, 8, 0, 0)
    T = event + dt.timedelta(hours=2)
    return assess(snapshot(T, caso_progresivo(T, event)), config())


def _clear(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    narrative._cache.clear()


def test_sin_api_key_hay_resumen_local_con_datos_reales(monkeypatch):
    _clear(monkeypatch)
    a = _assessment()
    n = narrative.narrate(a)

    assert n["source"] == "deterministic_fallback"
    assert n["findings"]
    assert {f["variable_code"] for f in n["findings"]} >= {"HR", "RR", "SpO2"}
    assert all(f["baseline"] is not None and f["latest"] is not None
               for f in n["findings"])
    assert set(n["evidence_refs"]) <= {c.record_id for c in a.citas}


def test_flag_desactiva_openai_sin_eliminar_la_clave(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    a = _assessment()

    def no_debe_llamarse(*_):
        raise AssertionError("el flag desactivado no debe invocar OpenAI")

    monkeypatch.setattr(narrative, "_call_openai", no_debe_llamarse)
    n = narrative.narrate(a, use_openai=False)

    assert n["source"] == "deterministic_fallback"
    assert narrative.narrative_status(False)["configured"] is True
    assert narrative.narrative_status(False)["enabled"] is False


def test_openai_solo_redacta_y_cita_evidencia_permitida(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    a = _assessment()
    rid = a.citas[0].record_id

    def fake_call(payload_json, api_key, model):
        assert api_key == "test-only"
        assert '"risk_score"' in payload_json
        return narrative.NarrativeText(
            title="Patron multivariable que amerita revision",
            summary=("Los datos muestran cambios concordantes y sostenidos en varias "
                     "mediciones, sin atribuir una causa clinica."),
            review_points=[
                "Revisar conjuntamente la evolucion de las mediciones destacadas.",
                "Confirmar la calidad y el contexto de los registros citados.",
            ],
            evidence_refs=[rid],
        )

    monkeypatch.setattr(narrative, "_call_openai", fake_call)
    n = narrative.narrate(a)

    assert n["source"] == "openai"
    assert n["model"] == "gpt-5.6-terra"
    assert n["evidence_refs"] == [rid]
    assert n["findings"][0]["baseline"] == round(a.dominante().mediana_baseline, 2)
    assert a.risk == _assessment().risk  # la narracion no modifica el dictamen


def test_una_cita_inventada_activa_el_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    a = _assessment()

    monkeypatch.setattr(
        narrative,
        "_call_openai",
        lambda *_: narrative.NarrativeText(
            title="Patron multivariable que amerita revision",
            summary=("Los datos muestran cambios concordantes y sostenidos que deben "
                     "contrastarse con la informacion disponible."),
            review_points=[
                "Revisar la evolucion de las mediciones destacadas.",
                "Confirmar la calidad de los registros citados.",
            ],
            evidence_refs=["OBS-INVENTADA"],
        ),
    )

    n = narrative.narrate(a)
    assert n["source"] == "deterministic_fallback"
    assert "OBS-INVENTADA" not in n["evidence_refs"]


def test_cifras_generadas_fuera_de_findings_activan_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    a = _assessment()

    monkeypatch.setattr(
        narrative,
        "_call_openai",
        lambda *_: narrative.NarrativeText(
            title="Patron con 99 hallazgos que amerita revision",
            summary=("Los datos muestran cambios concordantes y sostenidos que deben "
                     "contrastarse con la informacion disponible."),
            review_points=[
                "Revisar la evolucion de las mediciones destacadas.",
                "Confirmar la calidad de los registros citados.",
            ],
            evidence_refs=[],
        ),
    )

    assert narrative.narrate(a)["source"] == "deterministic_fallback"


def test_prompt_default_define_objetivo_y_limites_clinicos():
    prompt = narrative.DEFAULT_PROMPT.lower()
    assert "qué patrón muestran los datos" in prompt
    assert "debería revisar" in prompt
    assert "no diagnostiques" in prompt
    assert "evidence_refs_allowed" in prompt


def test_narrativa_historica_separa_maximo_y_cierre(monkeypatch):
    _clear(monkeypatch)
    peak = _assessment()
    current = replace(
        peak,
        T=peak.T + dt.timedelta(hours=4),
        risk=0.1,
        priority="LOW",
    )
    h = HistoryAnalysis(
        start=peak.T - dt.timedelta(hours=8),
        end=current.T,
        current=current,
        peak=peak,
        assessments=(peak, current),
        priority_counts={"LOW": 1, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 1},
        transitions=(
            PriorityTransition(peak.T, peak.priority, peak.risk),
            PriorityTransition(current.T, current.priority, current.risk),
        ),
        skipped_without_baseline=2,
    )

    n = narrative.narrate_history(h, use_openai=False)

    assert n["scope"] == "longitudinal_history"
    assert "máxima" in n["title"].lower()
    assert "cierre" in n["summary"].lower()
    assert set(n["evidence_refs"]) <= {c.record_id for c in (*peak.citas, *current.citas)}


def test_cliente_openai_usa_modelo_prompt_y_esquema_predeterminados(monkeypatch):
    import openai

    captured = {}
    expected = narrative.NarrativeText(
        title="Patrón sustentado para revisión profesional",
        summary=("Los datos muestran un patrón concordante que amerita contrastarse "
                 "con la valoración profesional y los registros disponibles."),
        review_points=[
            "Revisar la evolución conjunta de las mediciones destacadas.",
            "Confirmar la consistencia de los registros citados.",
        ],
        evidence_refs=[],
    )

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"output_parsed": expected})()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    result = narrative._call_openai("{}", "test-only", narrative.DEFAULT_MODEL)

    assert result == expected
    assert captured["model"] == "gpt-5.6-terra"
    assert captured["instructions"] == narrative.DEFAULT_PROMPT
    assert captured["text_format"] is narrative.NarrativeText
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["store"] is False
    assert captured["closed"] is True
