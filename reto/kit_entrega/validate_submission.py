#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, sys
from pathlib import Path
from datetime import datetime

SIGNALS_REQUIRED = {
    "signal_id","patient_id","decision_datetime","risk_score","priority_level",
    "evidence_start","evidence_end","explanation","model_version"
}
EVIDENCE_REQUIRED = {
    "signal_id","source_file","record_id","event_datetime","available_datetime","evidence_role"
}
PRIORITIES = {"LOW","MEDIUM","HIGH","CRITICAL"}
EVIDENCE_ROLES = {"PRIMARY","SUPPORTING","CONTEXT","QUALITY"}

def parse_dt(value: str) -> datetime:
    value=(value or "").strip()
    if not value:
        raise ValueError("empty datetime")
    try:
        return datetime.fromisoformat(value.replace("Z","+00:00"))
    except Exception as exc:
        raise ValueError(f"invalid datetime '{value}'") from exc

def read_csv(path: Path):
    with open(path,encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f))

def header(path: Path):
    with open(path,encoding="utf-8-sig",newline="") as f:
        return next(csv.reader(f), [])

def main():
    ap=argparse.ArgumentParser(
        description="HealthSignal LATAM — RISA Data V1.0 public submission-format validator"
    )
    ap.add_argument("results", help="Folder containing signals.csv and evidence.csv")
    ap.add_argument("--risa", help="Optional RISA V1.0 root to validate patient_id values")
    args=ap.parse_args()

    root=Path(args.results)
    errors=[]; warnings=[]; passes=[]
    sp=root/"signals.csv"; ep=root/"evidence.csv"

    if not sp.exists(): errors.append("Missing required file: signals.csv")
    if not ep.exists(): errors.append("Missing required file: evidence.csv")
    if errors:
        return report(passes,warnings,errors)

    sig_header=set(header(sp)); ev_header=set(header(ep))
    for c in sorted(SIGNALS_REQUIRED - sig_header):
        errors.append(f"signals.csv missing required column: {c}")
    for c in sorted(EVIDENCE_REQUIRED - ev_header):
        errors.append(f"evidence.csv missing required column: {c}")
    if errors:
        return report(passes,warnings,errors)
    passes.append("Required files and columns are present")

    signals=read_csv(sp)
    evidence=read_csv(ep)

    signal_ids=set()
    decision_by_signal={}
    patient_ids=set()

    for n,r in enumerate(signals, start=2):
        sid=(r.get("signal_id") or "").strip()
        pid=(r.get("patient_id") or "").strip()
        patient_ids.add(pid)

        if not sid:
            errors.append(f"signals.csv row {n}: empty signal_id")
        elif sid in signal_ids:
            errors.append(f"signals.csv row {n}: duplicate signal_id '{sid}'")
        signal_ids.add(sid)

        if not pid:
            errors.append(f"signals.csv row {n}: empty patient_id")

        try:
            score=float(r.get("risk_score",""))
            if not 0 <= score <= 1:
                raise ValueError
        except Exception:
            errors.append(f"signals.csv row {n}: risk_score must be numeric in [0,1]")

        priority=(r.get("priority_level") or "").strip().upper()
        if priority not in PRIORITIES:
            errors.append(f"signals.csv row {n}: invalid priority_level '{priority}'")

        confidence=(r.get("confidence_score") or "").strip()
        if confidence:
            try:
                conf=float(confidence)
                if not 0 <= conf <= 1:
                    raise ValueError
            except Exception:
                errors.append(f"signals.csv row {n}: confidence_score must be numeric in [0,1] or blank")

        try:
            ev_start=parse_dt(r.get("evidence_start",""))
            ev_end=parse_dt(r.get("evidence_end",""))
            decision=parse_dt(r.get("decision_datetime",""))
            decision_by_signal[sid]=decision
            if not (ev_start <= ev_end <= decision):
                errors.append(
                    f"signals.csv row {n}: require evidence_start <= evidence_end <= decision_datetime"
                )
        except ValueError as exc:
            errors.append(f"signals.csv row {n}: {exc}")

        if not (r.get("explanation") or "").strip():
            errors.append(f"signals.csv row {n}: explanation is required")
        if not (r.get("model_version") or "").strip():
            errors.append(f"signals.csv row {n}: model_version is required")

    linked={sid:0 for sid in signal_ids}
    for n,r in enumerate(evidence, start=2):
        sid=(r.get("signal_id") or "").strip()
        if sid not in signal_ids:
            errors.append(f"evidence.csv row {n}: unknown signal_id '{sid}'")
        else:
            linked[sid]+=1

        role=(r.get("evidence_role") or "").strip().upper()
        if role not in EVIDENCE_ROLES:
            errors.append(f"evidence.csv row {n}: invalid evidence_role '{role}'")

        if not (r.get("source_file") or "").strip():
            errors.append(f"evidence.csv row {n}: source_file is required")
        if not (r.get("record_id") or "").strip():
            errors.append(f"evidence.csv row {n}: record_id is required")

        try:
            parse_dt(r.get("event_datetime",""))
            available=parse_dt(r.get("available_datetime",""))
            decision=decision_by_signal.get(sid)
            if decision is not None and available > decision:
                errors.append(
                    f"evidence.csv row {n}: available_datetime is after signal decision_datetime"
                )
        except ValueError as exc:
            errors.append(f"evidence.csv row {n}: {exc}")

        contribution=(r.get("contribution") or "").strip()
        if contribution:
            try:
                float(contribution)
            except Exception:
                warnings.append(f"evidence.csv row {n}: contribution is not numeric")

    for sid,n in linked.items():
        if sid and n < 1:
            errors.append(f"Signal '{sid}' has no linked evidence")

    if args.risa:
        risa=Path(args.risa)
        hits=list(risa.rglob("patients.csv"))
        if len(hits) != 1:
            errors.append("--risa root must contain exactly one patients.csv")
        else:
            valid=set()
            with open(hits[0],encoding="utf-8-sig",newline="") as f:
                for r in csv.DictReader(f):
                    valid.add((r.get("patient_id") or "").strip())
            invalid=sorted(p for p in patient_ids if p and p not in valid)
            if invalid:
                errors.append(
                    f"{len(invalid)} patient_id value(s) not found in RISA; example: {invalid[:3]}"
                )
            else:
                passes.append("All patient_id values exist in supplied RISA data")

    passes.append(f"Parsed {len(signals)} signal row(s) and {len(evidence)} evidence row(s)")
    return report(passes,warnings,errors)

def report(passes,warnings,errors):
    print("HealthSignal LATAM — RISA V1.0 Submission Validator")
    print("="*58)
    for x in passes: print(f"[PASS] {x}")
    for x in warnings: print(f"[WARN] {x}")
    for x in errors: print(f"[FAIL] {x}")
    print("-"*58)
    if errors:
        print(f"INVALID SUBMISSION FORMAT — {len(errors)} error(s), {len(warnings)} warning(s)")
        code=2
    else:
        print(f"VALID SUBMISSION FORMAT — {len(warnings)} warning(s)")
        code=0
    print("Structure only: this validator contains no Gold Standard, hidden cases, or evaluation answers.")
    return code

if __name__=="__main__":
    sys.exit(main())
