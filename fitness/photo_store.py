"""Запись фотографий прогресса из веб-панели: загрузка файлов и правка подписи.

Отдельный слой записи: аналитический модуль web/readonly.py по-прежнему только читает.
Правила:
- тип файла определяется по содержимому (JPEG, PNG, WebP), а не по имени и не по заголовкам браузера;
- имя файла на диске генерируется здесь же; имя с компьютера пользователя не используется в пути;
- файлы создаются только внутри папки фото и только как новые (существующий файл не перезаписывается);
- загрузка нескольких файлов — всё или ничего: при любой ошибке созданные этой операцией файлы удаляются,
  а записи в базе не появляются (все строки вставляются одной транзакцией);
- одинаковое содержимое повторно не сохраняется.
"""

import hashlib
import os
import secrets
from datetime import date
from pathlib import Path

from . import validation as v
from .db import PHOTOS_DIR, transaction
from .errors import NotFoundError, ValidationError
from .measurements import PHOTO_VIEW_ALIASES, PHOTO_VIEWS, _clean_photo_path

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 12
MIN_FILE_BYTES = 64

# Расширение файла на диске выбирается по распознанному типу.
FORMATS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
FORMAT_NAMES = "JPEG, PNG, WebP"


def detect_format(data):
    """Тип изображения по первым байтам или None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _display_name(name):
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:80] or "без имени"


def check_image(data, original_name=""):
    """Проверить содержимое файла. Возвращает формат ('jpeg' | 'png' | 'webp')."""
    label = _display_name(original_name)
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ValidationError("file", f"файл «{label}» пустой")
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError("file", f"файл «{label}» больше {MAX_FILE_BYTES // (1024 * 1024)} МБ")
    fmt = detect_format(data)
    if fmt is None:
        if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc"):
            raise ValidationError("file", f"файл «{label}» в формате HEIC: браузер его не покажет. "
                                          f"Сохраните фото как JPEG (на телефоне: камера → формат JPEG)")
        raise ValidationError("file", f"файл «{label}» не похож на изображение; допустимо: {FORMAT_NAMES}")
    if len(data) < MIN_FILE_BYTES:
        raise ValidationError("file", f"файл «{label}» слишком маленький, похоже, повреждён")
    return fmt


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _find_existing_copy(conn, photos_dir, data):
    """id уже записанного фото с тем же содержимым или None. Сравниваются только файлы того же размера."""
    size, digest = len(data), None
    base = Path(photos_dir)
    for row in conn.execute("SELECT id, file_path FROM photos ORDER BY id"):
        path = base / Path(*row["file_path"].split("/"))
        try:
            if path.stat().st_size != size:
                continue
            digest = digest or _digest(data)
            if _digest(path.read_bytes()) == digest:
                return row["id"]
        except OSError:
            continue
    return None


def _new_file(photos_dir, taken_on, view, ext, data):
    """Создать новый файл с уникальным именем. Существующие файлы никогда не перезаписываются."""
    folder = Path(photos_dir) / taken_on[:4]
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        name = f"{taken_on}_{view}_{secrets.token_hex(4)}{ext}"
        target = folder / name
        try:
            with open(target, "xb") as fh:   # x — только создание нового файла
                try:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                except BaseException:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise
        except FileExistsError:
            continue
        return target, f"{taken_on[:4]}/{name}"
    raise OSError("не удалось подобрать свободное имя файла")


def save_uploads(conn, files, taken_on, notes=None, photos_dir=PHOTOS_DIR, today=None):
    """Сохранить загруженные фото одной датой.

    files: список (view, original_name, data). view — front | back | side | other (или русское название).
    Возвращает {"ids": [...], "records": [...]}. При ошибке ничего не сохраняется.
    """
    taken_on = v.parse_date(taken_on, "taken_on", today=today)
    notes = v.parse_text(notes, "notes")
    if not files:
        raise ValidationError("file", "выберите хотя бы один файл")
    if len(files) > MAX_FILES:
        raise ValidationError("file", f"за один раз можно загрузить не больше {MAX_FILES} файлов")

    prepared, seen = [], {}
    for view, original_name, data in files:
        view = v.parse_enum(view, "view", PHOTO_VIEWS, aliases=PHOTO_VIEW_ALIASES, required=True)
        fmt = check_image(data, original_name)
        digest = _digest(data)
        if digest in seen:
            raise ValidationError("file", f"файл «{_display_name(original_name)}» выбран дважды "
                                          f"(совпадает с «{seen[digest]}»)")
        seen[digest] = _display_name(original_name)
        prepared.append((view, original_name, data, FORMATS[fmt]))

    for view, original_name, data, _ in prepared:
        existing = _find_existing_copy(conn, photos_dir, data)
        if existing is not None:
            raise ValidationError("file", f"файл «{_display_name(original_name)}» уже загружен ранее "
                                          f"(фото №{existing}); повторно не сохранён")

    created = []
    try:
        for view, _, data, ext in prepared:
            path, rel = _new_file(photos_dir, taken_on, view, ext, data)
            created.append((path, view, rel))
            # Та же проверка пути, что у add_photo: файл внутри папки фото и существует.
            if _clean_photo_path(rel, photos_dir) != rel:
                raise ValidationError("file_path", "неожиданный путь файла")
        with transaction(conn):
            ids = [conn.execute("INSERT INTO photos (taken_on, file_path, view, notes) VALUES (?, ?, ?, ?)",
                                (taken_on, rel, view, notes)).lastrowid
                   for _, view, rel in created]
    except BaseException:
        for item in created:
            item[0].unlink(missing_ok=True)
        raise
    records = [dict(conn.execute("SELECT * FROM photos WHERE id = ?", (i,)).fetchone()) for i in ids]
    return {"ids": ids, "records": records}


def get_photo(conn, photo_id):
    pid = v.parse_number(photo_id, "id", integer=True, min_value=1, required=True)
    row = conn.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise NotFoundError(f"Фото №{pid} не найдено")
    return dict(row)


def update_photo(conn, photo_id, taken_on, view, notes, expected_updated_at=None, today=None):
    """Изменить дату, ракурс и комментарий. Файл не трогается. view=None — ракурс не указан."""
    taken_on = v.parse_date(taken_on, "taken_on", today=today)
    view = v.parse_enum(view, "view", PHOTO_VIEWS, aliases=PHOTO_VIEW_ALIASES)
    notes = v.parse_text(notes, "notes")
    with transaction(conn):
        current = get_photo(conn, photo_id)
        if expected_updated_at is not None and current["updated_at"] != expected_updated_at:
            raise ValidationError("photo", "фото изменилось после открытия формы; откройте его заново")
        conn.execute("UPDATE photos SET taken_on = ?, view = ?, notes = ? WHERE id = ?",
                     (taken_on, view, notes, current["id"]))
    return get_photo(conn, current["id"])


def today_iso():
    return date.today().isoformat()
