-- Миграция 003: точность времени Health Connect до наносекунд.
-- Время записей хранится как 'YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ' (UTC, всегда ровно 9 цифр доли секунды).
-- Фиксированная ширина нужна, чтобы строковые сравнения и сортировка совпадали с хронологическими:
-- на них держатся CHECK (end_utc > start_utc), MAX(end_utc), UNIQUE и ORDER BY.
-- Меняются только проверки формата времени в пяти таблицах; все остальные ограничения, индексы,
-- триггеры и представления воспроизводятся без изменений. Существующие значения получают суффикс '.000000000'.
-- Столбцы deleted_at, created_at, updated_at, imported_at, exported_at (собственные метки трекера) остаются с точностью до секунды.
--
-- SQLite не умеет менять CHECK, поэтому таблицы пересоздаются: сначала новые копии под временными именами,
-- затем удаление старых (дочерние раньше родительских, чтобы каскады не удалили копируемые данные) и переименование.
-- Строки переносятся вместе с id, поэтому внешние ключи остаются верными.

-- Представления зависят от пересоздаваемых таблиц.
DROP VIEW v_health_sync_status;
DROP VIEW v_daily_sleep;
DROP VIEW v_daily_heart_rate;
DROP VIEW v_heart_rate_records;
DROP VIEW v_daily_steps;

-- step_intervals
CREATE TABLE step_intervals_new (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),   -- metadata.id в Health Connect
    client_record_id      TEXT,                                       -- metadata.clientRecordId, если источник его задаёт
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    count                 INTEGER NOT NULL CHECK (count BETWEEN 1 AND 1000000),   -- допустимый диапазон Health Connect
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc > start_utc)
);
INSERT INTO step_intervals_new (id, source_id, hc_id, client_record_id, client_record_version, start_utc, end_utc, start_offset_min, end_offset_min, local_date, count, hc_last_modified, payload_hash, revision, batch_id, deleted_at, created_at, updated_at)
SELECT id, source_id, hc_id, client_record_id, client_record_version, substr(start_utc, 1, 19) || '.000000000Z', substr(end_utc, 1, 19) || '.000000000Z', start_offset_min, end_offset_min, local_date, count, substr(hc_last_modified, 1, 19) || '.000000000Z', payload_hash, revision, batch_id, deleted_at, created_at, updated_at
FROM step_intervals;

-- heart_rate_records
CREATE TABLE heart_rate_records_new (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),
    client_record_id      TEXT,
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc >= start_utc)   -- Health Connect допускает запись пульса с равными началом и концом
);
INSERT INTO heart_rate_records_new (id, source_id, hc_id, client_record_id, client_record_version, start_utc, end_utc, start_offset_min, end_offset_min, local_date, hc_last_modified, payload_hash, revision, batch_id, deleted_at, created_at, updated_at)
SELECT id, source_id, hc_id, client_record_id, client_record_version, substr(start_utc, 1, 19) || '.000000000Z', substr(end_utc, 1, 19) || '.000000000Z', start_offset_min, end_offset_min, local_date, substr(hc_last_modified, 1, 19) || '.000000000Z', payload_hash, revision, batch_id, deleted_at, created_at, updated_at
FROM heart_rate_records;

-- heart_rate_samples
CREATE TABLE heart_rate_samples_new (
    id         INTEGER PRIMARY KEY,
    record_id  INTEGER NOT NULL REFERENCES heart_rate_records_new (id) ON DELETE CASCADE,
    time_utc   TEXT NOT NULL CHECK (time_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    bpm        INTEGER NOT NULL CHECK (bpm BETWEEN 1 AND 300),   -- диапазон Health Connect
    UNIQUE (record_id, time_utc)
);
INSERT INTO heart_rate_samples_new (id, record_id, time_utc, bpm)
SELECT id, record_id, substr(time_utc, 1, 19) || '.000000000Z', bpm
FROM heart_rate_samples;

-- sleep_sessions
CREATE TABLE sleep_sessions_new (
    id                    INTEGER PRIMARY KEY,
    source_id             INTEGER NOT NULL REFERENCES data_sources (id),
    hc_id                 TEXT NOT NULL CHECK (length(hc_id) > 0),
    client_record_id      TEXT,
    client_record_version INTEGER,
    start_utc             TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    end_utc               TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    start_offset_min      INTEGER CHECK (start_offset_min IS NULL OR start_offset_min BETWEEN -840 AND 840),
    end_offset_min        INTEGER CHECK (end_offset_min IS NULL OR end_offset_min BETWEEN -840 AND 840),
    local_date            TEXT NOT NULL CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),   -- дата пробуждения
    title                 TEXT,
    notes                 TEXT,
    hc_last_modified      TEXT NOT NULL CHECK (hc_last_modified GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    payload_hash          TEXT NOT NULL CHECK (length(payload_hash) = 64),
    revision              INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    batch_id              INTEGER NOT NULL REFERENCES import_batches (id),
    deleted_at            TEXT CHECK (deleted_at IS NULL OR deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_id, hc_id),
    CHECK (end_utc >= start_utc)
);
INSERT INTO sleep_sessions_new (id, source_id, hc_id, client_record_id, client_record_version, start_utc, end_utc, start_offset_min, end_offset_min, local_date, title, notes, hc_last_modified, payload_hash, revision, batch_id, deleted_at, created_at, updated_at)
SELECT id, source_id, hc_id, client_record_id, client_record_version, substr(start_utc, 1, 19) || '.000000000Z', substr(end_utc, 1, 19) || '.000000000Z', start_offset_min, end_offset_min, local_date, title, notes, substr(hc_last_modified, 1, 19) || '.000000000Z', payload_hash, revision, batch_id, deleted_at, created_at, updated_at
FROM sleep_sessions;

-- sleep_stages
CREATE TABLE sleep_stages_new (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sleep_sessions_new (id) ON DELETE CASCADE,
    -- Типы стадий Health Connect (SleepSessionRecord.STAGE_TYPE_*).
    stage       TEXT NOT NULL CHECK (stage IN ('unknown', 'awake', 'sleeping', 'out_of_bed', 'light', 'deep', 'rem',
                                              'awake_in_bed')),
    start_utc   TEXT NOT NULL CHECK (start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    end_utc     TEXT NOT NULL CHECK (end_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z'),
    UNIQUE (session_id, start_utc),
    CHECK (end_utc > start_utc)
);
INSERT INTO sleep_stages_new (id, session_id, stage, start_utc, end_utc)
SELECT id, session_id, stage, substr(start_utc, 1, 19) || '.000000000Z', substr(end_utc, 1, 19) || '.000000000Z'
FROM sleep_stages;

-- Удаление старых таблиц: сначала дочерние, чтобы ON DELETE CASCADE не затронул переносимые данные.
DROP TABLE heart_rate_samples;
DROP TABLE heart_rate_records;
DROP TABLE step_intervals;
DROP TABLE sleep_stages;
DROP TABLE sleep_sessions;

ALTER TABLE step_intervals_new RENAME TO step_intervals;
ALTER TABLE heart_rate_records_new RENAME TO heart_rate_records;
ALTER TABLE heart_rate_samples_new RENAME TO heart_rate_samples;
ALTER TABLE sleep_sessions_new RENAME TO sleep_sessions;
ALTER TABLE sleep_stages_new RENAME TO sleep_stages;

CREATE INDEX idx_step_intervals_span ON step_intervals (source_id, start_utc, end_utc);

CREATE INDEX idx_step_intervals_date ON step_intervals (local_date);

CREATE INDEX idx_step_intervals_hc_id ON step_intervals (hc_id);   -- удаление в Health Connect приходит только как id

CREATE INDEX idx_step_intervals_client ON step_intervals (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

CREATE INDEX idx_heart_rate_records_span ON heart_rate_records (source_id, start_utc, end_utc);

CREATE INDEX idx_heart_rate_records_date ON heart_rate_records (local_date);

CREATE INDEX idx_heart_rate_records_hc_id ON heart_rate_records (hc_id);

CREATE INDEX idx_heart_rate_records_client ON heart_rate_records (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

CREATE INDEX idx_sleep_sessions_span ON sleep_sessions (source_id, start_utc, end_utc);

CREATE INDEX idx_sleep_sessions_date ON sleep_sessions (local_date);

CREATE INDEX idx_sleep_sessions_hc_id ON sleep_sessions (hc_id);

CREATE INDEX idx_sleep_sessions_client ON sleep_sessions (source_id, client_record_id) WHERE client_record_id IS NOT NULL;

CREATE TRIGGER trg_step_intervals_updated AFTER UPDATE ON step_intervals
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE step_intervals SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_heart_rate_records_updated AFTER UPDATE ON heart_rate_records
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE heart_rate_records SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_sleep_sessions_updated AFTER UPDATE ON sleep_sessions
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE sleep_sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

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

CREATE VIEW v_daily_heart_rate AS
SELECT r.local_date, r.source_id,
       MIN(x.bpm) AS min_bpm, ROUND(AVG(x.bpm), 1) AS avg_bpm, MAX(x.bpm) AS max_bpm, COUNT(*) AS samples
FROM heart_rate_samples x JOIN heart_rate_records r ON r.id = x.record_id
WHERE r.deleted_at IS NULL
GROUP BY r.local_date, r.source_id;

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
