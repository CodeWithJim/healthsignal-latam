"""Rutas del proyecto. Único lugar donde se resuelven ubicaciones en disco."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RAW = ROOT / "data" / "raw"
CONFIG = ROOT / "config"
SOURCES_YAML = CONFIG / "sources.yaml"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
WAREHOUSE = ROOT / "data" / "warehouse.duckdb"
RESULTS = ROOT / "results"
MANIFEST = ROOT / "MANIFEST_SHA256.txt"

# Prefijo con el que el manifiesto oficial nombra la raíz del dataset.
MANIFEST_PREFIX = "01_RISA_DATA_V1_0/"


def validador() -> Path | None:
    """Ruta a validate_submission.py del kit oficial, esté donde esté."""
    hits = sorted(ROOT.rglob("validate_submission.py"))
    return hits[0] if hits else None


def raw_path(source_file: str) -> Path:
    """'03_monitoring/vital_signs.csv' -> ruta absoluta en data/raw."""
    return RAW / source_file


def sql_literal(path: Path) -> str:
    """Ruta utilizable dentro de una cadena SQL de DuckDB."""
    return str(path).replace("\\", "/").replace("'", "''")
