-- Миграция 004: ожидающие удаления Health Connect.
-- Удаление (список deleted) может прийти раньше записи: файл с удалением принят, а файл с самой записью найден позже
-- (--allow-older) или записи у трекера пока нет. Строки записей для такого удаления создать нельзя (не известны тип,
-- время и источник записи), поэтому факт удаления хранится отдельно и проверяется, когда запись появляется.
--
-- Область удаления такая же, как при обычном удалении: источники из sources файла (source_key) или, если их нет,
-- телефон файла (phone_id). Ровно одно из двух полей заполнено. Ключ источника хранится текстом, а не ссылкой:
-- файл только с удалениями не должен создавать строки в data_sources.
-- exported_at — время экспорта файла с удалением (с точностью до секунды, как в import_batches): запись из файла,
-- экспортированного не позже, считается повтором старых данных и не восстанавливается.
-- Миграция только добавляет таблицу и индексы; существующие данные не затрагиваются.

CREATE TABLE health_pending_deletions (
    id           INTEGER PRIMARY KEY,
    source_key   TEXT CHECK (source_key IS NULL OR length(source_key) > 0),
    phone_id     TEXT CHECK (phone_id IS NULL OR length(phone_id) > 0),
    hc_id        TEXT NOT NULL CHECK (length(hc_id) > 0),
    batch_id     INTEGER NOT NULL REFERENCES import_batches (id),
    exported_at  TEXT NOT NULL CHECK (exported_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((source_key IS NULL) <> (phone_id IS NULL))
);
CREATE UNIQUE INDEX uq_pending_deletions_source ON health_pending_deletions (source_key, hc_id) WHERE source_key IS NOT NULL;
CREATE UNIQUE INDEX uq_pending_deletions_phone ON health_pending_deletions (phone_id, hc_id) WHERE phone_id IS NOT NULL;
CREATE INDEX idx_pending_deletions_batch ON health_pending_deletions (batch_id);
