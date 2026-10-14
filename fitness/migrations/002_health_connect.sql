-- Миграция 002: данные из Health Connect (шаги, пульс, сон) от часов Amazfit через Zepp.
-- Только новые объекты; существующие таблицы и данные не меняются.
-- Время: TEXT 'YYYY-MM-DDTHH:MM:SSZ' (UTC, точность до секунды); смещение пояса в минутах (может быть NULL);
-- local_date — местная дата, которую вычисляет импорт: для шагов и пульса дата начала, для сна дата пробуждения.
-- Неизвестные значения хранятся как NULL. Границы значений взяты из исходного кода Health Connect (androidx).

-- ---------- Источники и журнал импортов ----------

CREATE TABLE data_sources (
    id                   INTEGER PRIMARY KEY,
    -- Один непустой ключ вместо UNIQUE по набору столбцов: в SQLite NULL в UNIQUE не защищает от дублей.
    source_key           TEXT NOT NULL UNIQUE CHECK (length(source_key) > 0),
    platform             TEXT NOT NULL CHECK (platform IN ('health_connect')),
    package_name         TEXT NOT NULL CHECK (length(package_name) > 0),
    device_manufacturer  TEXT,
    device_model         TEXT,
    device_type          TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE import_batches (
    id              INTEGER PRIMARY KEY,
    file_name       TEXT NOT NULL CHECK (length(file_name) > 0),
    sha256          TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    format_version  INTEGER NOT NULL CHECK (format_version >= 1),
    phone_id        TEXT NOT NULL CHECK (length(phone_id) > 0),
    sequence        INTEGER NOT NULL CHECK (sequence >= 1),
    exported_at     TEXT NOT NULL CHECK (exported_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    imported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error           TEXT,
    CHECK ((status = 'failed') = (error IS NOT NULL))
);
-- Повторный успешный файл и повторный номер отклоняются; после неудачи файл можно импортировать снова.
CREATE UNIQUE INDEX uq_import_batches_ok_sha256 ON import_batches (sha256) WHERE status = 'ok';
CREATE UNIQUE INDEX uq_import_batches_ok_sequence ON import_batches (phone_id, sequence) WHERE status = 'ok';

CREATE TABLE import_batch_counts (
    batch_id     INTEGER NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    record_type  TEXT NOT NULL CHECK (record_type IN ('steps', 'heart_rate', 'sleep')),
    inserted     INTEGER NOT NULL DEFAULT 0 CHECK (inserted >= 0),
    updated      INTEGER NOT NULL DEFAULT 0 CHECK (updated >= 0),
    unchanged    INTEGER NOT NULL DEFAULT 0 CHECK (unchanged >= 0),
    deleted      INTEGER NOT NULL DEFAULT 0 CHECK (deleted >= 0),
    rejected     INTEGER NOT NULL DEFAULT 0 CHECK (rejected >= 0),
    PRIMARY KEY (batch_id, record_type)
);

-- ---------- Шаги ----------

CREATE TABLE step_intervals (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),   -- metadata.id в Health Connect
    client_record_id      TEXT,                                       -- metadata.clientRecordId, если источник его задаёт
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    count                 INTEGER NOT NULL CHECK (count BETWEEN 1 AND 1000000),   -- допустимый диапазон Health Connect
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc > start_utc)
);
-- Не уникальный: нужен для поиска подозрительных дублей с новым hc_id и для выборок по времени.
CREATE INDEX idx_step_intervals_span ON step_intervals (source_id, start_utc, end_utc);
CREATE INDEX idx_step_intervals_date ON step_intervals (local_date);
CREATE INDEX idx_step_intervals_hc_id ON step_intervals (hc_id);   -- удаление в Health Connect приходит только как id
CREATE INDEX idx_step_intervals_client ON step_intervals (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

-- ---------- Пульс ----------

CREATE TABLE heart_rate_records (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),
    client_record_id      TEXT,
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc >= start_utc)   -- Health Connect допускает запись пульса с равными началом и концом
);
CREATE INDEX idx_heart_rate_records_span ON heart_rate_records (source_id, start_utc, end_utc);
CREATE INDEX idx_heart_rate_records_date ON heart_rate_records (local_date);
CREATE INDEX idx_heart_rate_records_hc_id ON heart_rate_records (hc_id);
CREATE INDEX idx_heart_rate_records_client ON heart_rate_records (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

CREATE TABLE heart_rate_samples (
    id         INTEGER PRIMARY KEY,
    record_id  INTEGER NOT NULL REFERENCES heart_rate_records (id) ON DELETE CASCADE,
    time_utc   TEXT NOT NULL CHECK (time_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    bpm        INTEGER NOT NULL CHECK (bpm BETWEEN 1 AND 300),   -- диапазон Health Connect
    UNIQUE (record_id, time_utc)
);

-- ---------- Сон ----------

CREATE TABLE sleep_sessions (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),
    client_record_id      TEXT,
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),   -- дата пробуждения
    title                 TEXT,
    notes                 TEXT,
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc >= start_utc)
);
CREATE INDEX idx_sleep_sessions_span ON sleep_sessions (source_id, start_utc, end_utc);
CREATE INDEX idx_sleep_sessions_date ON sleep_sessions (local_date);
CREATE INDEX idx_sleep_sessions_hc_id ON sleep_sessions (hc_id);
CREATE INDEX idx_sleep_sessions_client ON sleep_sessions (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

CREATE TABLE sleep_stages (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sleep_sessions (id) ON DELETE CASCADE,
    -- Типы стадий Health Connect (SleepSessionRecord.STAGE_TYPE_*).
    stage       TEXT NOT NULL CHECK (stage IN ('unknown', 'awake', 'sleeping', 'out_of_bed', 'light', 'deep', 'rem',
                                              'awake_in_bed')),
    start_utc   TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    end_utc     TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    UNIQUE (session_id, start_utc),
    CHECK (end_utc > start_utc)
);

-- ---------- Автообновление updated_at ----------

CREATE TRIGGER trg_step_intervals_updated AFTER UPDATE ON step_intervals
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE step_intervals SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_heart_rate_records_updated AFTER UPDATE ON heart_rate_records
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE heart_rate_records SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_sleep_sessions_updated AFTER UPDATE ON sleep_sessions
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE sleep_sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

-- ---------- Представления (только чтение; удалённые записи скрыты) ----------

-- Источники не складываются: какой источник считать, решает слой MCP.
CREATE VIEW v_daily_steps AS
SELECT s.local_date, s.source_id, d.package_name,
       SUM(s.count) AS steps, COUNT(*) AS intervals,
       MIN(s.start_utc) AS first_start_utc, MAX(s.end_utc) AS last_end_utc
FROM step_intervals s JOIN data_sources d ON d.id = s.source_id
WHERE s.deleted_at IS NULL
GROUP BY s.local_date, s.source_id;

CREATE VIEW v_heart_rate_records AS
SELECT r.id, r.source_id, r.hc_id, r.start_utc, r.end_utc,
       CAST(round((julianday(r.end_utc) - julianday(r.start_utc)) * 86400) AS INTEGER) AS duration_sec,
       r.local_date,
       (SELECT COUNT(*) FROM heart_rate_samples x WHERE x.record_id = r.id) AS samples
FROM heart_rate_records r
WHERE r.deleted_at IS NULL;

-- Дата берётся из записи (начало интервала); у записей длиной около минуты отсчёты на границе суток не расходятся.
CREATE VIEW v_daily_heart_rate AS
SELECT r.local_date, r.source_id,
       MIN(x.bpm) AS min_bpm, ROUND(AVG(x.bpm), 1) AS avg_bpm, MAX(x.bpm) AS max_bpm, COUNT(*) AS samples
FROM heart_rate_samples x JOIN heart_rate_records r ON r.id = x.record_id
WHERE r.deleted_at IS NULL
GROUP BY r.local_date, r.source_id;

-- Дата пробуждения. Минуты по стадиям неизвестны (NULL), если у сессии нет стадий.
CREATE VIEW v_daily_sleep AS
SELECT s.local_date, s.source_id,
       COUNT(*) AS sessions,
       MIN(s.start_utc) AS first_start_utc, MAX(s.end_utc) AS last_end_utc,
       ROUND(SUM((julianday(s.end_utc) - julianday(s.start_utc)) * 1440), 1) AS session_min,
       ROUND(SUM(st.asleep_min), 1) AS asleep_min,
       ROUND(SUM(st.awake_min), 1) AS awake_min,
       ROUND(SUM(st.light_min), 1) AS light_min,
       ROUND(SUM(st.deep_min), 1) AS deep_min,
       ROUND(SUM(st.rem_min), 1) AS rem_min
FROM sleep_sessions s
LEFT JOIN (
    SELECT session_id,
           SUM(CASE WHEN stage IN ('light', 'deep', 'rem', 'sleeping') THEN m END) AS asleep_min,
           SUM(CASE WHEN stage IN ('awake', 'out_of_bed', 'awake_in_bed') THEN m END) AS awake_min,
           SUM(CASE WHEN stage = 'light' THEN m END) AS light_min,
           SUM(CASE WHEN stage = 'deep' THEN m END) AS deep_min,
           SUM(CASE WHEN stage = 'rem' THEN m END) AS rem_min
    FROM (SELECT session_id, stage, (julianday(end_utc) - julianday(start_utc)) * 1440 AS m FROM sleep_stages)
    GROUP BY session_id
) st ON st.session_id = s.id
WHERE s.deleted_at IS NULL
GROUP BY s.local_date, s.source_id;

-- Время последней записи по типам и последнего успешного импорта (общее для всех типов).
CREATE VIEW v_health_sync_status AS
SELECT 'steps' AS record_type,
       (SELECT MAX(end_utc) FROM step_intervals WHERE deleted_at IS NULL) AS last_record_end_utc,
       (SELECT COUNT(*) FROM step_intervals WHERE deleted_at IS NULL) AS records,
       (SELECT MAX(imported_at) FROM import_batches WHERE status = 'ok') AS last_ok_import_at,
       (SELECT MAX(exported_at) FROM import_batches WHERE status = 'ok') AS last_exported_at
UNION ALL
SELECT 'heart_rate',
       (SELECT MAX(end_utc) FROM heart_rate_records WHERE deleted_at IS NULL),
       (SELECT COUNT(*) FROM heart_rate_records WHERE deleted_at IS NULL),
       (SELECT MAX(imported_at) FROM import_batches WHERE status = 'ok'),
       (SELECT MAX(exported_at) FROM import_batches WHERE status = 'ok')
UNION ALL
SELECT 'sleep',
       (SELECT MAX(end_utc) FROM sleep_sessions WHERE deleted_at IS NULL),
       (SELECT COUNT(*) FROM sleep_sessions WHERE deleted_at IS NULL),
       (SELECT MAX(imported_at) FROM import_batches WHERE status = 'ok'),
       (SELECT MAX(exported_at) FROM import_batches WHERE status = 'ok');
