-- Миграция 005: корзина фотографий.
-- При удалении фото через веб-панель файл переносится из data/photos в data/photos_trash, а запись — из photos
-- сюда, со всеми полями: по ним фото восстанавливается в исходном виде (дата, ракурс, комментарий, замер, время
-- создания и правки). photo_id — номер фото до удаления; при восстановлении он возвращается, если свободен.
-- file_path — путь относительно data/photos до удаления, trash_path — путь файла относительно data/photos_trash.
-- session_id хранится без внешнего ключа: замер могут удалить, пока фото в корзине (тогда связь не восстановится).
-- deleted_at — время переноса в корзину.
-- Миграция только добавляет таблицу и индекс; существующие записи photos не затрагиваются.

CREATE TABLE photo_trash (
    id          INTEGER PRIMARY KEY,
    photo_id    INTEGER NOT NULL CHECK (photo_id > 0),
    session_id  INTEGER,
    taken_on    TEXT NOT NULL CHECK (taken_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    file_path   TEXT NOT NULL CHECK (length(file_path) > 0),
    trash_path  TEXT NOT NULL UNIQUE CHECK (length(trash_path) > 0),
    view        TEXT CHECK (view IS NULL OR view IN ('front', 'side', 'back', 'other')),
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_photo_trash_deleted ON photo_trash (deleted_at);
