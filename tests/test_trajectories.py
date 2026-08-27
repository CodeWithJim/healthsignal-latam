"""CA-01 a CA-06 — los criterios de aceptación del analizador.

Trayectorias sintéticas, sin leer un CSV. Si estas seis formas no se distinguen,
lo que el motor produzca sobre los datos reales no importa.
"""
from __future__ import annotations

import datetime as dt

import pytest

from hs.domain.scoring import assess
from trayectorias import (caso_estable, caso_progresivo, config, contexto, pico,
                          plano, rampa, serie, snapshot)

CFG = config()
EVENTO = dt.datetime(2026, 7, 10, 8, 0, 0)       # las 08:00 del ejemplo oficial


# --------------------------------------------------------------- CA-01

def test_ca01_deterioro_progresivo_dispara_temprano():
    """La trayectoria oficial debe disparar entre las 09:00 y las 10:00.

    Antes de que ninguna lectura aislada cruce un umbral convencional: a las
    09:00 el paciente está en HR 94, SpO2 94, RR 20, TEMP 37.3.
    """
    t10 = EVENTO + dt.timedelta(hours=2)          # 10:00
    a = assess(snapshot(t10, caso_progresivo(t10, EVENTO)), CFG)

    assert a.k >= 3, f"se esperaban 3+ canales concordantes, hubo {a.k}: {a.concordantes}"
    assert a.priority in ("HIGH", "CRITICAL"), (
        f"a las 10:00 debía priorizarse; dio {a.priority} con riesgo {a.risk:.3f}\n{a.explicacion}")
    assert not a.supresiones, f"no debía suprimirse: {[s.regla for s in a.supresiones]}"


def test_ca01_a_las_11_no_es_mas_tarde_de_lo_necesario():
    """A las 11:00 el cuadro es evidente: debe seguir priorizado y con más puntaje."""
    t10 = EVENTO + dt.timedelta(hours=2)
    t11 = EVENTO + dt.timedelta(hours=3)
    a10 = assess(snapshot(t10, caso_progresivo(t10, EVENTO)), CFG)
    a11 = assess(snapshot(t11, caso_progresivo(t11, EVENTO)), CFG)
    assert a11.risk > a10.risk, "el riesgo debe crecer al consolidarse el patrón"
    assert a11.priority in ("HIGH", "CRITICAL")


def test_ca01_la_evidencia_apunta_a_registros_reales():
    t10 = EVENTO + dt.timedelta(hours=2)
    a = assess(snapshot(t10, caso_progresivo(t10, EVENTO)), CFG)
    assert a.citas, "toda señal debe llevar evidencia"
    assert any(c.role == "PRIMARY" for c in a.citas)
    for c in a.citas:
        assert c.available_time <= a.T, f"{c.record_id} no estaba disponible en T"
        assert c.source_file and c.record_id


# --------------------------------------------------------------- CA-02

def test_ca02_pico_por_actividad_fisica_no_alerta():
    """HR +40 con contexto de actividad y retorno rápido: no es una señal."""
    T = dt.datetime(2026, 7, 10, 16, 0, 0)
    ini = T - dt.timedelta(minutes=40)
    canales = caso_estable(T)
    canales["HR"] = serie("HR", T, pico("HR", ini, 128.0, 40), seed=1)
    ctx = contexto("CONTEXT", "PHYSICAL_ACTIVITY", "HIGH", ini - dt.timedelta(minutes=10),
                   T, "CTX-0009999")

    a = assess(snapshot(T, canales, intervalos=[ctx]), CFG)
    assert a.priority not in ("HIGH", "CRITICAL"), (
        f"un pico explicado por actividad no debe priorizarse: {a.explicacion}")
    reglas = {s.regla for s in a.supresiones}
    assert reglas & {"actividad", "transitorio"}, f"debía registrarse supresión: {a.explicacion}"
    citadas = {c.record_id for s in a.supresiones for c in s.citas}
    assert "CTX-0009999" in citadas or a.priority == "LOW"


# --------------------------------------------------------------- CA-03

def test_ca03_caida_aislada_de_spo2_no_alerta():
    """Una muestra a 71 % que vuelve a 96 %: artefacto, no evento."""
    T = dt.datetime(2026, 7, 10, 16, 0, 0)
    canales = caso_estable(T)
    canales["SpO2"] = serie("SpO2", T, pico("SpO2", T - dt.timedelta(minutes=20),
                                            71.0, 1), seed=3)

    a = assess(snapshot(T, canales), CFG)
    assert a.priority not in ("HIGH", "CRITICAL"), a.explicacion
    assert any(s.regla == "transitorio" for s in a.supresiones) or a.k == 0, a.explicacion


# --------------------------------------------------------------- CA-04

def test_ca04_desconexion_no_sube_el_riesgo_y_baja_la_confianza():
    """La ausencia de datos degrada la confianza; nunca incrementa el riesgo (P-06)."""
    T = dt.datetime(2026, 7, 10, 16, 0, 0)
    hueco = (T - dt.timedelta(hours=4), T - dt.timedelta(minutes=20))

    completo = assess(snapshot(T, caso_estable(T)), CFG)
    parcial = assess(snapshot(
        T,
        {c: serie(c, T, plano(c), seed=10 + i, huecos=[hueco])
         for i, c in enumerate(("HR", "RR", "SpO2", "TEMP"))},
        intervalos=[contexto("CONNECTIVITY", "DISCONNECTED", "DISCONNECTED",
                             hueco[0], hueco[1], "CONN-000999")]), CFG)

    assert parcial.risk <= completo.risk + 1e-9, "un bache no puede subir el riesgo"
    assert parcial.confidence < completo.confidence, "un bache debe bajar la confianza"
    assert parcial.priority != "CRITICAL"


def test_ca04_cobertura_insuficiente_acota_la_prioridad():
    """Aun con deterioro real, poca cobertura impide CRITICAL y cita el evento."""
    T = EVENTO + dt.timedelta(hours=3)
    hueco = (T - dt.timedelta(hours=5), T - dt.timedelta(minutes=40))
    destinos = {"HR": 108.0, "RR": 25.0, "SpO2": 91.0, "TEMP": 38.0}
    canales = {c: serie(c, T, rampa(c, EVENTO, v, 3), seed=1 + i, huecos=[hueco])
               for i, (c, v) in enumerate(destinos.items())}
    a = assess(snapshot(T, canales, intervalos=[
        contexto("CONNECTIVITY", "DISCONNECTED", "DISCONNECTED", hueco[0], hueco[1],
                 "CONN-000777")]), CFG)
    assert a.priority != "CRITICAL", a.explicacion


# --------------------------------------------------------------- CA-05

def test_ca05_ruido_plano_no_emite_senal():
    T = dt.datetime(2026, 7, 10, 16, 0, 0)
    for seed in (10, 40, 70, 100):
        a = assess(snapshot(T, caso_estable(T, seed=seed)), CFG)
        assert a.priority == "LOW", f"seed {seed}: {a.explicacion}"
        assert a.risk < 0.4, f"seed {seed}: riesgo {a.risk:.3f}"


# --------------------------------------------------------------- CA-06

def test_ca06_un_canal_extremo_puntua_menos_que_cuatro_moderados():
    """P-07 hecho aritmética: la concordancia pesa más que la magnitud."""
    T = EVENTO + dt.timedelta(hours=2)
    concordante = assess(snapshot(T, caso_progresivo(T, EVENTO)), CFG)

    solo = caso_estable(T)
    solo["HR"] = serie("HR", T, rampa("HR", EVENTO, 175.0, 3), seed=1)
    aislado = assess(snapshot(T, solo), CFG)

    assert aislado.k == 1, f"debía haber un solo canal concordante, hubo {aislado.k}"
    assert aislado.risk < concordante.risk, (
        f"un canal a {aislado.canales['HR'].nivel:+.1f}s ({aislado.risk:.3f}) no puede "
        f"superar a {concordante.k} canales concordantes ({concordante.risk:.3f})")
    assert aislado.priority != "CRITICAL", "un solo canal jamás alcanza CRITICAL"


# --------------------------------------------------------------- invariantes

def test_sin_baseline_el_canal_no_participa():
    """RF-05: no se asume normalidad cuando falta historia."""
    T = dt.datetime(2026, 7, 10, 16, 0, 0)
    canales = caso_estable(T)
    canales["SpO2"] = serie("SpO2", T, plano("SpO2"), horas=6.5, seed=3)
    a = assess(snapshot(T, canales), CFG)
    assert "SpO2" not in a.canales
    assert "SpO2" not in a.explicacion


def test_el_puntaje_esta_en_rango_y_la_explicacion_es_reproducible():
    T = EVENTO + dt.timedelta(hours=2)
    a = assess(snapshot(T, caso_progresivo(T, EVENTO)), CFG)
    b = assess(snapshot(T, caso_progresivo(T, EVENTO)), CFG)
    assert 0.0 <= a.risk <= 1.0 and 0.0 <= a.confidence <= 1.0
    assert a.priority in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert a.explicacion == b.explicacion, "misma entrada, misma explicación (P-05)"
    assert a.risk == b.risk


@pytest.mark.parametrize("horas", [1, 2, 3])
def test_la_senal_crece_de_forma_monotona_durante_el_deterioro(horas):
    prev = None
    for h in range(1, horas + 1):
        T = EVENTO + dt.timedelta(hours=h)
        a = assess(snapshot(T, caso_progresivo(T, EVENTO)), CFG)
        if prev is not None:
            assert a.risk >= prev - 1e-6, f"el riesgo cayó en h={h}"
        prev = a.risk
