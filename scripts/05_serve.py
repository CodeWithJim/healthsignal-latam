"""Levanta la API de consulta y decisión en vivo.

    .venv\\Scripts\\python.exe scripts\\05_serve.py [--port 8000]

Documentación interactiva en http://127.0.0.1:8000/docs
El endpoint que importa: /decide?patient=PAT-0869&at=2026-07-20T18:00:00
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    print(f"  docs    http://{a.host}:{a.port}/docs")
    print(f"  decide  http://{a.host}:{a.port}/decide?patient=PAT-0869&at=2026-07-20T18:00:00")
    uvicorn.run("hs.api:app", host=a.host, port=a.port, reload=a.reload)
