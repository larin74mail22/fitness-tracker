"""Корзина фотографий: перенос в корзину, восстановление и окончательное удаление (только из веб-панели).

Файл лежит в data/photos, пока фото в галерее, и в data/photos_trash, пока фото в корзине. Запись — в таблице
photos или photo_trash (миграция 005) со всеми полями, поэтому восстановленное фото получает прежние дату, ракурс,
комментарий и, если номер свободен, прежний номер.

Порядок каждой операции выбран так, чтобы при сбое на любом шаге файл не пропал, а запись не ссылалась
на отсутствующий файл:
1. проверки: подтверждение, путь внутри своей папки, файл на месте; при удалении — резервная копия базы и её проверка;
2. копия файла на новое место (жёсткая ссылка или копирование), сверка содержимого; исходный файл ещё на месте;
3. одна транзакция: запись переходит из одной таблицы в другую; при ошибке созданная копия удаляется;
4. исходный файл удаляется. Если это не удалось, данные целы, а лишняя копия называется в ответе.
Существующие файлы никогда не перезаписываются: при совпадении имени выбирается имя с суффиксом -2, -3 и т. д.
Пути из базы проверяются: только относительные, без '..', без диска, внутри своей папки, расширение изображения.
"""

import hashlib
import os
import secrets
import shutil
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import records
from . import validation as v
from .db import DATA_DIR, PHOTOS_DIR, backup, transaction, verify_backup_file
from .errors import BackupError, ConfirmationError, NotFoundError, PhotoFileError, ValidationError
from .measurements import PHOTO_EXTENSIONS
from .photo_store import _digest, _find_existing_copy, check_image, get_photo

TRASH_DIR = DATA_DIR / "photos_trash"
_MAX_NAME_TRIES = 50


def safe_path(base, rel, where):
    """Абсолютный путь файла rel внутри папки base. Иначе — PhotoFileError (ничего не меняется)."""
    if not isinstance(rel, str) or not rel.strip() or len(rel) > 260 or "\x00" in rel:
        raise PhotoFileError(f"Недопустимый путь файла в {where}.")
    candidate = PureWindowsPath(rel)
    if candidate.is_absolute() or candidate.drive or candidate.root or ".." in candidate.parts:
        raise PhotoFileError(f"Путь «{rel}» ведёт за пределы {where}; операция не выполнена.")
    if candidate.suffix.lower() not in PHOTO_EXTENSIONS:
        raise PhotoFileError(f"Файл «{rel}» не похож на фото; операция не выполнена.")
    root = Path(base).resolve()
    full = (root / Path(*candidate.parts)).resolve()
    if root not in full.parents:
        raise PhotoFileError(f"Путь «{rel}» ведёт за пределы {where}; операция не выполнена.")
    return full


def _copy_new(src, dst):
    """Скопировать src в новый файл dst. Существующий dst — FileExistsError."""
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        try:
            shutil.copyfileobj(fin, fout)
            fout.flush()
            os.fsync(fout.fileno())
        except BaseException:
            fout.close()
            dst.unlink(missing_ok=True)
            raise


def _place_copy(src, dst):
    """Создать dst с тем же содержимым, что у src. src не меняется, существующий dst не перезаписывается."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)                 # тот же диск: мгновенно; существующий dst -> FileExistsError
    except FileExistsError:
        raise
    except OSError:
        _copy_new(src, dst)               # ссылки не поддерживаются — обычная копия


def _candidates(rel):
    path = PurePosixPath(rel)
    yield rel
    for n in range(2, _MAX_NAME_TRIES + 1):
        yield str(path.with_name(f"{path.stem}-{n}{path.suffix}"))


def _copy_to_free_name(src, base, rel, digest, where, taken):
    """Положить копию src в base под именем rel или, если оно занято, rel-2, rel-3...

    Возвращает (относительный путь, абсолютный путь). Содержимое копии сверяется с digest.
    taken(rel) — занято ли имя в базе (на диске проверяется само создание файла).
    """
    for cand in _candidates(rel):
        target = safe_path(base, cand, where)
        if taken(cand) or target.exists():
            continue
        try:
            _place_copy(src, target)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PhotoFileError(f"Не удалось перенести файл в {where}: {exc}. Ничего не изменено.") from None
        try:
            same = _digest(target.read_bytes()) == digest
        except OSError:
            same = False
        if not same:
            target.unlink(missing_ok=True)
            raise PhotoFileError(f"Копия файла в {where} не совпала с оригиналом; ничего не изменено.")
        return cand, target
    raise PhotoFileError(f"Не удалось подобрать свободное имя файла в {where}; ничего не изменено.")


def _remove_original(path, where):
    """Удалить исходный файл после успешной записи. Ошибка не откатывает операцию: данные уже целы."""
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"Исходный файл не удалён ({exc}): лишняя копия осталась в {where}: {path}"
    return None


def _check_code(code, expected, what):
    if not isinstance(code, str) or code.strip().upper() != expected:
        raise ConfirmationError(f"Неверный или устаревший код подтверждения ({what}). Откройте страницу заново.")


def _make_backup(conn, backup_dir, label, table, record_id, updated_at):
    # Копия делается до транзакции: backup() внутри BEGIN IMMEDIATE того же соединения зависает.
    try:
        path = backup(conn, backup_dir, label=label)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"Резервная копия не создана: {exc}. Ничего не изменено.") from None
    verify_backup_file(path, table, record_id, updated_at)
    return path


# ---------- Перенос в корзину ----------

def move_to_trash(conn, photo_id, confirmation_code, photos_dir=PHOTOS_DIR, trash_dir=TRASH_DIR, backup_dir=None):
    """Перенести фото в корзину после подтверждения и проверенной резервной копии базы.

    Код подтверждения — тот же, что выдаёт records.prepare_delete для фото (меняется при правке фото).
    """
    photo = get_photo(conn, photo_id)
    expected = records.prepare_delete(conn, "photo", photo["id"])["confirmation_code"]
    _check_code(confirmation_code, expected, "удаление фото")
    source = safe_path(photos_dir, photo["file_path"], "папки data/photos")
    if not source.is_file():
        raise PhotoFileError(f"Файл фото №{photo['id']} не найден: data/photos/{photo['file_path']}. "
                             "Запись не удалена; проверьте папку вручную.")
    digest = _digest(source.read_bytes())
    backup_path = _make_backup(conn, backup_dir, f"before-delete-photo-{photo['id']}", "photos",
                               photo["id"], photo["updated_at"])

    def taken(rel):
        return conn.execute("SELECT 1 FROM photo_trash WHERE trash_path = ?", (rel,)).fetchone() is not None

    trash_rel, trash_file = _copy_to_free_name(source, trash_dir, photo["file_path"], digest,
                                               "папки data/photos_trash", taken)
    try:
        with transaction(conn):
            trash_id = conn.execute(
                "INSERT INTO photo_trash (photo_id, session_id, taken_on, file_path, trash_path, view, notes,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo["id"], photo["session_id"], photo["taken_on"], photo["file_path"], trash_rel, photo["view"],
                 photo["notes"], photo["created_at"], photo["updated_at"])).lastrowid
            deleted = conn.execute("DELETE FROM photos WHERE id = ? AND updated_at = ?",
                                   (photo["id"], photo["updated_at"])).rowcount
            if deleted != 1:
                raise PhotoFileError(f"Фото №{photo['id']} изменилось во время удаления; ничего не изменено.")
    except BaseException:
        trash_file.unlink(missing_ok=True)
        raise
    return {"trash_id": trash_id, "photo_id": photo["id"], "trash_path": trash_rel, "backup": str(backup_path),
            "warning": _remove_original(source, "data/photos")}


# ---------- Чтение корзины ----------

def get_trash_item(conn, trash_id):
    tid = v.parse_number(trash_id, "id", integer=True, min_value=1, required=True)
    row = conn.execute("SELECT * FROM photo_trash WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise NotFoundError(f"Фото №{tid} в корзине не найдено")
    return dict(row)


def trash_file(item, trash_dir=TRASH_DIR):
    """Путь файла корзины или None, если путь недопустим или файла нет."""
    try:
        path = safe_path(trash_dir, item["trash_path"], "папки data/photos_trash")
    except PhotoFileError:
        return None
    return path if path.is_file() else None


def purge_code(item, file_present):
    # Код меняется, если запись корзины или наличие файла изменились после показа страницы.
    raw = (f"trash:{item['id']}:{item['photo_id']}:{item['trash_path']}:{item['deleted_at']}:"
           f"{item['updated_at']}:{int(bool(file_present))}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()


# ---------- Восстановление ----------

def restore(conn, trash_id, photos_dir=PHOTOS_DIR, trash_dir=TRASH_DIR):
    """Вернуть фото из корзины в галерею с прежними полями. Существующие файлы не перезаписываются."""
    item = get_trash_item(conn, trash_id)
    source = safe_path(trash_dir, item["trash_path"], "папки data/photos_trash")
    if not source.is_file():
        raise PhotoFileError(f"Файл не найден в корзине: data/photos_trash/{item['trash_path']}. "
                             "Фото не восстановлено, запись корзины не изменена.")
    data = source.read_bytes()
    check_image(data, item["trash_path"])
    duplicate = _find_existing_copy(conn, photos_dir, data)
    if duplicate is not None:
        raise ValidationError("photo", f"такое же фото уже есть в галерее (фото №{duplicate}); "
                                       "восстанавливать копию не нужно")

    def taken(rel):
        return conn.execute("SELECT 1 FROM photos WHERE file_path = ?", (rel,)).fetchone() is not None

    rel, target = _copy_to_free_name(source, photos_dir, item["file_path"], _digest(data),
                                     "папки data/photos", taken)
    try:
        with transaction(conn):
            pid = item["photo_id"]
            if conn.execute("SELECT 1 FROM photos WHERE id = ?", (pid,)).fetchone():
                pid = None                       # номер уже занят новым фото — запись получит новый
            session = item["session_id"]
            if session is not None and not conn.execute(
                    "SELECT 1 FROM measurement_sessions WHERE id = ?", (session,)).fetchone():
                session = None                   # замер удалён, пока фото было в корзине
            new_id = conn.execute(
                "INSERT INTO photos (id, session_id, taken_on, file_path, view, notes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, session, item["taken_on"], rel, item["view"], item["notes"], item["created_at"],
                 item["updated_at"])).lastrowid
            removed = conn.execute("DELETE FROM photo_trash WHERE id = ? AND trash_path = ?",
                                   (item["id"], item["trash_path"])).rowcount
            if removed != 1:
                raise PhotoFileError("Запись корзины изменилась во время восстановления; ничего не изменено.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"photo_id": new_id, "file_path": rel, "same_id": new_id == item["photo_id"],
            "warning": _remove_original(source, "data/photos_trash")}


# ---------- Окончательное удаление ----------

def prepare_purge(conn, trash_id, trash_dir=TRASH_DIR):
    item = get_trash_item(conn, trash_id)
    present = trash_file(item, trash_dir) is not None
    return {"item": item, "file_present": present, "confirmation_code": purge_code(item, present)}


def purge(conn, trash_id, confirmation_code, trash_dir=TRASH_DIR, backup_dir=None):
    """Удалить запись корзины и её файл навсегда — после подтверждения и проверенной резервной копии базы."""
    item = get_trash_item(conn, trash_id)
    path = safe_path(trash_dir, item["trash_path"], "папки data/photos_trash")
    present = path.is_file()
    _check_code(confirmation_code, purge_code(item, present), "окончательное удаление")
    backup_path = _make_backup(conn, backup_dir, f"before-purge-photo-{item['photo_id']}", "photo_trash",
                               item["id"], item["updated_at"])
    pending = None
    if present:
        # Файл сначала переименовывается (в той же папке корзины): при ошибке базы его можно вернуть.
        pending = path.with_name(f"{path.name}.deleting-{secrets.token_hex(4)}")
        try:
            os.rename(path, pending)
        except OSError as exc:
            raise PhotoFileError(f"Файл корзины не удалён: {exc}. Ничего не изменено.") from None
    try:
        with transaction(conn):
            removed = conn.execute("DELETE FROM photo_trash WHERE id = ? AND trash_path = ?",
                                   (item["id"], item["trash_path"])).rowcount
            if removed != 1:
                raise PhotoFileError("Запись корзины изменилась; ничего не удалено.")
    except BaseException:
        if pending is not None:
            try:
                os.rename(pending, path)
            except OSError as exc:
                raise PhotoFileError(f"Запись корзины не удалена, а файл не вернулся на место ({exc}): "
                                     f"он лежит как {pending}. Переименуйте его обратно в {path.name}.") from None
        raise
    warning = None
    if pending is not None:
        try:
            pending.unlink()
        except OSError as exc:
            warning = f"Запись удалена, но файл не удалился ({exc}): {pending}"
    return {"purged": True, "trash_id": item["id"], "backup": str(backup_path), "warning": warning}
