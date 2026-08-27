"""Verificación de integridad de los archivos de origen.

Implementa P-08 y RNF-04: cada corrida comprueba el SHA-256 de los 17 CSV
contra MANIFEST_SHA256.txt y se detiene ante una discrepancia.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from . import paths

_LINE = re.compile(r"^(?P<path>\S+)\s*\|\s*(?P<bytes>\d+)\s*\|\s*(?P<sha>[0-9a-f]{64})\s*$")


class IntegrityError(RuntimeError):
    """El contenido de un archivo de origen no coincide con el manifiesto."""


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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(source_files: list[str], strict: bool = True) -> dict[str, dict]:
    """Compara cada fuente contra el manifiesto.

    Devuelve {source_file: {sha256, sha256_expected, sha256_ok, bytes}}.
    Con strict=True, lanza IntegrityError si alguna difiere.
    """
    exp = expected()
    report: dict[str, dict] = {}
    bad: list[str] = []

    for sf in source_files:
        p = paths.raw_path(sf)
        if not p.exists():
            raise FileNotFoundError(f"Falta el archivo de origen: {p}")
        actual = sha256(p)
        e = exp.get(sf)
        ok = None if e is None else (actual == e[1])
        report[sf] = {
            "sha256": actual,
            "sha256_expected": None if e is None else e[1],
            "sha256_ok": ok,
            "bytes": p.stat().st_size,
        }
        if ok is False:
            bad.append(sf)

    if bad and strict:
        raise IntegrityError(
            "Los siguientes archivos no coinciden con MANIFEST_SHA256.txt: " + ", ".join(bad)
        )
    return report


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
