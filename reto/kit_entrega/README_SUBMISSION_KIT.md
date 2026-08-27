# HealthSignal LATAM — RISA Data V1.0
## Submission Kit público

Este kit valida únicamente la **estructura técnica de la entrega**. No contiene Gold Standard, casos ocultos, rankings esperados, thresholds oficiales ni respuestas de evaluación.

### Archivos
- `signals_template.csv`
- `evidence_template.csv`
- `validate_submission.py`

### Uso
1. Copie las plantillas a la carpeta `results/`.
2. Renómbrelas como `signals.csv` y `evidence.csv`.
3. Complete los resultados de su solución.
4. Ejecute:

```bash
python validate_submission.py results/
```

Opcionalmente, para comprobar que los `patient_id` declarados existen en la copia local de RISA:

```bash
python validate_submission.py results/ --risa /ruta/a/RISA_DATA_V1.0
```

### Importante
`VALID SUBMISSION FORMAT` significa únicamente que la estructura es válida. El validador **no mide desempeño**, no revela casos ocultos y no compara contra el Gold Standard.
