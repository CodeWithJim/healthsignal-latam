# Material recibido de la organización

Todo lo que hay bajo `reto/` proviene del paquete oficial del CIP Hackathon —
Área 1, escenario RISA (Red Integrada de Salud Andina). **No se modifica.** El
código de la solución vive en `src/`, `scripts/`, `config/`, `tests/` y `ui/`.

Las carpetas originales llegaron como exportación de Google Drive, con una capa
de anidación y un sufijo de marca temporal (`…-20260826T224649Z-1-001/`) que
introdujo el exportador, no la organización. Esa capa se retiró; los archivos
son byte a byte los recibidos.

| Ruta | Contenido | Versionado |
|---|---|---|
| `documentos/` | Los 3 PDF oficiales del reto más el enunciado `DESAFIO_Salud.pdf` | sí |
| `kit_entrega/` | Plantillas de salida y el validador oficial `validate_submission.py` | sí |
| `dataset_original/` | RISA Data V1.0 · 17 CSV · 245 MB | **no** (`.gitignore`) |

## `kit_entrega/validate_submission.py`

Es el validador oficial y el único ejemplar en el repositorio. `hs.paths.validador()`
lo localiza por búsqueda desde la raíz, y `tests/test_contract.py` lo ejecuta
contra `results/` como parte de la suite.

Su SHA-256 es `7d97a787f856e974a7f9b1f19ec7163282f6d33642b4a8d3e1fb9be3dcbccfc9`,
que es el que declara `MANIFEST_SHA256.txt`. Existía una segunda copia en la raíz
del repositorio con finales de línea CRLF; su hash no coincidía con el manifiesto
y se eliminó.

## `dataset_original/` frente a `data/raw/`

Son el mismo contenido, verificado byte a byte. `data/raw/` es la copia que
consume el pipeline y la única que las rutas de `hs.paths` resuelven;
`dataset_original/` se conserva como respaldo del paquete tal como llegó. Ninguna
de las dos se versiona.
