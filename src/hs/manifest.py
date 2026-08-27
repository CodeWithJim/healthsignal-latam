"""Verificación de integridad de los archivos de origen.

Implementa P-08 y RNF-04. El control no es "el archivo es idéntico al que
mandaron" sino algo más útil: **lo que ya procesamos sigue intacto**.

La diferencia importa cuando llega información nueva. Un archivo al que se le
agregan filas al final es un caso legítimo y frecuente; uno al que le editaron
una fila vieja, no. Hashear el archivo entero no los distingue: los dos dan
"distinto". Hashear el **prefijo** —los bytes que ya habíamos leído— separa uno
del otro, y de paso caza el caso peor, que es editar una fila vieja mientras se
agregan nuevas.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from . import paths

_LINE = re.compile(r"^(?P<path>\S+)\s*\|\s*(?P<bytes>\d+)\s*\|\s*(?P<sha>[0-9a-f]{64})\s*$")

# Veredicto de una fuente respecto de la última ingesta registrada.
IDENTICO = "IDENTICO"            # mismos bytes, mismo contenido
APENDADO = "APENDADO"            # creció y el prefijo quedó intacto: información nueva
NUEVA_VERSION = "NUEVA_VERSION"  # cambió por completo, pero coincide con el manifiesto oficial
MODIFICADO = "MODIFICADO"        # el prefijo cambió y nadie lo respalda: se editó algo procesado
NUEVO = "NUEVO"                  # no hay ingesta previa de esta fuente


class IntegrityError(RuntimeError):
    """Se alteró contenido que el sistema ya había procesado."""


def expected() -> dict[str, tuple[int, str]]:
    """Lee el manifiesto oficial. Devuelve {'01_master/patients.csv': (bytes, sha256)}."""
    out: dict[str, tuple[int, str]] = {}
    if not paths.MANIFEST.exists():
        return out
    for line in paths.MANIFEST.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        p = m.group("path")
        if p.startswith(paths.MANIFEST_PREFIX):
            out[p[len(paths.MANIFEST_PREFIX):]] = (int(m.group("bytes")), m.group("sha"))
    return out


def sha256(path: Path, hasta: int | None = None) -> str:
    """SHA-256 del archivo, o de sus primeros `hasta` bytes."""
    h = hashlib.sha256()
    restante = hasta
    with open(path, "rb") as f:
        while True:
            n = 1 << 20 if restante is None else min(1 << 20, restante)
            if n <= 0:
                break
            chunk = f.read(n)
            if not chunk:
                break
            h.update(chunk)
            if restante is not None:
                restante -= len(chunk)
    return h.hexdigest()


def clasificar(path: Path, actual_sha: str, actual_bytes: int,
               previo: tuple[int, str] | None, sha_oficial: str | None = None) -> str:
    """Compara una fuente contra el estado con el que se la ingestó por última vez.

    El manifiesto oficial es la autoridad externa. Si el contenido cambió por
    completo pero coincide con lo que la organización declara, es una entrega
    nueva —no una edición— por más que no se parezca a lo que teníamos. Sin esa
    distinción, publicar la versión congelada del dataset detendría el sistema
    como si alguien hubiera manipulado los archivos.
    """
    if previo is None:
        return NUEVO
    bytes_previos, sha_previo = previo

    if actual_bytes == bytes_previos and actual_sha == sha_previo:
        return IDENTICO
    if actual_bytes > bytes_previos and sha256(path, bytes_previos) == sha_previo:
        return APENDADO
    if sha_oficial is not None and actual_sha == sha_oficial:
        return NUEVA_VERSION
    return MODIFICADO


def verify(source_files: list[str], previo: dict[str, tuple[int, str]] | None = None,
           strict: bool = True) -> dict[str, dict]:
    """Compara cada fuente contra el manifiesto oficial y contra la última ingesta.

    Devuelve por fuente: sha256, sha256_expected, sha256_ok, bytes, estado.

    Con `strict`, sólo `MODIFICADO` detiene el proceso. Que una fuente crezca no
    es un error: es el caso que el sistema tiene que soportar.
    """
    exp = expected()
    previo = previo or {}
    report: dict[str, dict] = {}
    alterados: list[str] = []

    for sf in source_files:
        p = paths.raw_path(sf)
        if not p.exists():
            raise FileNotFoundError(f"Falta el archivo de origen: {p}")
        actual = sha256(p)
        n = p.stat().st_size
        e = exp.get(sf)
        estado = clasificar(p, actual, n, previo.get(sf), None if e is None else e[1])
        report[sf] = {
            "sha256": actual,
            "sha256_expected": None if e is None else e[1],
            "sha256_ok": None if e is None else (actual == e[1]),
            "bytes": n,
            "estado": estado,
        }
        if estado == MODIFICADO:
            alterados.append(sf)

    if alterados and strict:
        raise IntegrityError(
            "Se alteró contenido ya procesado en: " + ", ".join(alterados))
    return report


def primera_ingesta_valida(report: dict[str, dict]) -> list[str]:
    """Fuentes que se ingestan por primera vez y no coinciden con el manifiesto oficial.

    Sólo aplica a la primera vez: después, la referencia pasa a ser el estado con
    el que se ingestó, porque el archivo puede haber crecido legítimamente.
    """
    return [sf for sf, v in report.items()
            if v["estado"] == NUEVO and v["sha256_ok"] is False]


def git_sha() -> str | None:
    """Commit actual, o None si el repositorio todavía no tiene commits."""
    try:
        r = subprocess.run(
            ["git", "-C", str(paths.ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None
