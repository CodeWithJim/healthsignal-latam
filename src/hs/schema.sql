-- Esquema del almacén analítico — HealthSignal LATAM
-- Implementa plan.md §3. Las restricciones son el mecanismo, no la documentación:
-- P-02 (causalidad temporal) y RS-02..RS-04 (contrato de salida) viven aquí.

-- ===========================================================================
-- CAPA CLEAN
-- ===========================================================================

-- Observaciones: punto en el tiempo con valor.
-- Unifica vital_signs + wearable_observations + device_observations + laboratory_results.
CREATE TABLE IF NOT EXISTS observations (
    source_file     VARCHAR   NOT NULL,   -- '03_monitoring/vital_signs.csv'
    record_id       VARCHAR   NOT NULL,   -- 'OBS-0000000001'  -> esto ES evidence.csv
    patient_id      VARCHAR   NOT NULL,
    encounter_id    VARCHAR,
    device_id       VARCHAR,
    variable_code   VARCHAR   NOT NULL,
    domain          VARCHAR   NOT NULL,   -- VITAL | WEARABLE | LAB | DEVICE

    event_time      TIMESTAMP NOT NULL,   -- cuándo ocurrió
    available_time  TIMESTAMP NOT NULL,   -- cuándo se pudo saber

    value_num       DOUBLE,               -- unidad canónica (RD-04)
    value_text      VARCHAR,              -- ACTIVITY_LEVEL, SLEEP_STATE
    value_raw       DOUBLE,               -- lo que decía el CSV
    unit_raw        VARCHAR,
    unit_canonical  VARCHAR,

    source_system   VARCHAR,
    quality_flag    VARCHAR,              -- el de origen: señal, no filtro (RD-06)
    is_plausible    BOOLEAN   NOT NULL,   -- gate propio vs variable_catalog
    is_duplicate    BOOLEAN   NOT NULL,   -- retransmisión detectada (RD-05)
    ref_low         DOUBLE,
    ref_high        DOUBLE,

    PRIMARY KEY (source_file, record_id),
    -- P-02: la regla de oro como restricción de base, no como disciplina.
    CHECK (available_time >= event_time)
);

-- Intervalos: rango con estado.
-- Unifica patient_context + connectivity_events + medication_administrations + encounters.
CREATE TABLE IF NOT EXISTS intervals (
    source_file     VARCHAR   NOT NULL,
    record_id       VARCHAR   NOT NULL,
    patient_id      VARCHAR   NOT NULL,
    device_id       VARCHAR,
    kind            VARCHAR   NOT NULL,   -- CONTEXT | CONNECTIVITY | MEDICATION | ENCOUNTER
    subtype         VARCHAR,
    value_text      VARCHAR,
    start_time      TIMESTAMP NOT NULL,
    end_time        TIMESTAMP NOT NULL,
    available_time  TIMESTAMP NOT NULL,
    confidence      DOUBLE,
    extra_json      VARCHAR,

    PRIMARY KEY (source_file, record_id),
    CHECK (end_time >= start_time),
    CHECK (available_time >= start_time - INTERVAL 1 SECOND)
);

-- Hechos clínicos: antecedentes.
CREATE TABLE IF NOT EXISTS clinical_facts (
    source_file     VARCHAR   NOT NULL,
    record_id       VARCHAR   NOT NULL,
    patient_id      VARCHAR   NOT NULL,
    category        VARCHAR,
    onset_date      DATE,
    available_time  TIMESTAMP NOT NULL,   -- recorded_datetime
    status          VARCHAR,
    severity        VARCHAR,
    source_system   VARCHAR,
    PRIMARY KEY (source_file, record_id)
);

-- Cuarentena: filas que NO pudieron cargarse. RF-02 — nada se descarta en silencio.
-- Las filas implausibles o duplicadas SÍ se cargan, marcadas con su flag, para
-- que sigan siendo citables como evidencia QUALITY.
CREATE TABLE IF NOT EXISTS quarantine (
    source_file     VARCHAR NOT NULL,
    record_id       VARCHAR,
    patient_id      VARCHAR,
    variable_code   VARCHAR,
    reason          VARCHAR NOT NULL,   -- BAD_EVENT_TIME | BAD_AVAILABLE_TIME |
                                        -- TIME_ORDER | NO_PATIENT | NO_RECORD_ID |
                                        -- UNIT_UNKNOWN | NO_VALUE
    detail          VARCHAR,
    raw_row         VARCHAR
);

-- Manifiesto de ingesta: integridad de origen (P-08, RNF-04) e invariante de RF-02.
CREATE TABLE IF NOT EXISTS ingest_manifest (
    run_id            VARCHAR   NOT NULL,
    source_file       VARCHAR   NOT NULL,
    sha256            VARCHAR   NOT NULL,
    sha256_expected   VARCHAR,
    sha256_ok         BOOLEAN,
    bytes             BIGINT,
    rows_read         BIGINT,
    rows_loaded       BIGINT,
    rows_quarantined  BIGINT,
    target_table      VARCHAR,
    ingested_at       TIMESTAMP NOT NULL,
    git_sha           VARCHAR
);

-- ===========================================================================
-- CAPA RESULTS — el contrato del validador oficial, como restricciones
-- ===========================================================================

CREATE TABLE IF NOT EXISTS signals (
    signal_id         VARCHAR   PRIMARY KEY,
    patient_id        VARCHAR   NOT NULL,
    decision_datetime TIMESTAMP NOT NULL,
    risk_score        DOUBLE    NOT NULL,
    confidence_score  DOUBLE,
    priority_level    VARCHAR   NOT NULL,
    evidence_start    TIMESTAMP NOT NULL,
    evidence_end      TIMESTAMP NOT NULL,
    explanation       VARCHAR   NOT NULL,
    model_version     VARCHAR   NOT NULL,
    run_id            VARCHAR   NOT NULL,

    CHECK (risk_score BETWEEN 0 AND 1),                                  -- RS-02
    CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1),
    CHECK (priority_level IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    CHECK (length(trim(explanation)) > 0),
    CHECK (length(trim(model_version)) > 0),
    CHECK (evidence_start <= evidence_end),                              -- RS-03
    CHECK (evidence_end <= decision_datetime)
);

CREATE TABLE IF NOT EXISTS evidence (
    signal_id          VARCHAR   NOT NULL REFERENCES signals(signal_id),  -- RS-04
    source_file        VARCHAR   NOT NULL,
    record_id          VARCHAR   NOT NULL,
    variable_code      VARCHAR,
    event_datetime     TIMESTAMP NOT NULL,
    available_datetime TIMESTAMP NOT NULL,
    evidence_role      VARCHAR   NOT NULL,
    contribution       DOUBLE,

    CHECK (evidence_role IN ('PRIMARY','SUPPORTING','CONTEXT','QUALITY'))
);
