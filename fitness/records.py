"""Операции над записями любых категорий: поиск, состояние базы, удаление с подтверждением."""

import hashlib
import sqlite3

from . import validation as v
from .alcohol import get_alcohol_entry
from .db import backup, latest_schema_version, resolve_db_path, schema_version, transaction, verify_backup_file
from .errors import BackupError, ConfirmationError, NotFoundError
from .measurements import get_measurement
from .notes import get_note
from .nutrition import get_meal
from .workouts import get_workout

# Тип записи -> таблица, столбец даты, функция чтения. Имена таблиц берутся только отсюда.
RECORD_TYPES = {
    "measurement": ("measurement_sessions", "measured_on", get_measurement),
    "workout": ("workouts", "workout_date", get_workout),
    "meal": ("meals", "eaten_on", get_meal),
    "alcohol": ("alcohol_entries", "consumed_on", get_alcohol_entry),
    "note": ("notes", "note_date", get_note),
    "photo": ("photos", "taken_on", None),
}
SEARCH_TYPES = ("measurement", "workout", "meal", "alcohol", "note")

# Для каждого типа: SELECT id, дата, краткое описание, текст для поиска.
_SEARCH_SQL = {
    "measurement": (
        "SELECT s.id, s.measured_on AS date,"
        " COALESCE('вес ' || s.weight_kg || ' кг', '') ||"
        " COALESCE(' замеров: ' || (SELECT COUNT(*) FROM body_measurements b WHERE b.session_id = s.id), '')"
        " AS summary,"
        " COALESCE(s.notes, '') AS haystack"
        " FROM measurement_sessions s WHERE 1 = 1"
    ),
    "workout": (
        "SELECT w.id, w.workout_date AS date, w.title AS summary,"
        " w.title || ' ' || COALESCE(w.notes, '') || ' ' || COALESCE((SELECT group_concat("
        "   e.name || ' ' || COALESCE(we.notes, '') || ' ' || COALESCE(we.discomfort, ''), ' ')"
        "   FROM workout_exercises we JOIN exercises e ON e.id = we.exercise_id WHERE we.workout_id = w.id), '')"
        " AS haystack"
        " FROM workouts w WHERE 1 = 1"
    ),
    "meal": (
        "SELECT m.id, m.eaten_on AS date,"
        " COALESCE((SELECT group_concat(i.name, ', ') FROM meal_items i WHERE i.meal_id = m.id), '') AS summary,"
        " COALESCE(m.notes, '') || ' ' ||"
        " COALESCE((SELECT group_concat(i.name, ' ') FROM meal_items i WHERE i.meal_id = m.id), '') AS haystack"
        " FROM meals m WHERE 1 = 1"
    ),
    "alcohol": (
        "SELECT a.id, a.consumed_on AS date,"
        " a.drink || COALESCE(' ' || a.volume_ml || ' мл', '') AS summary,"
        " a.drink || ' ' || COALESCE(a.notes, '') AS haystack"
        " FROM alcohol_entries a WHERE 1 = 1"
    ),
    "note": (
        "SELECT n.id, n.note_date AS date, n.category || ': ' || substr(n.text, 1, 120) AS summary,"
        " n.category || ' ' || n.text || ' ' || COALESCE((SELECT group_concat("
        "   f.key || ' ' || COALESCE(f.value_text, f.value_num, ''), ' ')"
        "   FROM note_fields f WHERE f.note_id = n.id), '') AS haystack"
        " FROM notes n WHERE 1 = 1"
    ),
}
_DATE_COLUMN = {"measurement": "s.measured_on", "workout": "w.workout_date", "meal": "m.eaten_on",
                "alcohol": "a.consumed_on", "note": "n.note_date"}


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_records(conn, *, record_types=None, date_from=None, date_to=None, query=None,
                   note_category=None, limit=50):
    """Поиск по типам записей, периоду и словам. Нужен хотя бы один фильтр."""
    types = [v.parse_enum(t, "record_types", SEARCH_TYPES) for t in
             v.parse_list(record_types, "record_types", max_items=len(SEARCH_TYPES))] or list(SEARCH_TYPES)
    text = v.parse_text(query, "query", max_len=v.MAX_SHORT_TEXT)
    category = v.parse_text(note_category, "note_category", max_len=50)
    limit = v.parse_number(limit, "limit", integer=True, min_value=1, max_value=200)
    if date_from is not None or date_to is not None:
        start, end = v.parse_date_range(date_from, date_to)
    else:
        start = end = None
    if text is None and start is None and category is None and record_types is None:
        raise v.ValidationError("search", "укажите хотя бы один фильтр: период, слова, тип записей или категорию")
    if category is not None:
        types = [t for t in types if t == "note"]

    results = []
    for rtype in types:
        sql = _SEARCH_SQL[rtype]
        params = []
        if start is not None:
            sql += f" AND {_DATE_COLUMN[rtype]} BETWEEN ? AND ?"
            params += [start, end]
        if category is not None:
            sql += " AND n.category = ?"
            params.append(category.lower())
        sql = f"SELECT * FROM ({sql})"
        if text is not None:
            sql += " WHERE py_lower(haystack) LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(text.lower())}%")
        sql += " ORDER BY date DESC, id DESC LIMIT ?"
        params.append(limit)
        for row in conn.execute(sql, params):
            results.append({"record_type": rtype, "id": row["id"], "date": row["date"],
                            "summary": row["summary"].strip()})

    results.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    truncated = len(results) > limit
    return {"count": min(len(results), limit), "truncated": truncated, "results": results[:limit]}


def get_record(conn, record_type, record_id):
    rtype = v.parse_enum(record_type, "record_type", tuple(RECORD_TYPES), required=True)
    rid = v.parse_number(record_id, "id", integer=True, min_value=1, required=True)
    table, _, reader = RECORD_TYPES[rtype]
    if reader is not None:
        return reader(conn, rid)
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise NotFoundError(f"Запись {rtype} с id={rid} не найдена")
    return dict(row)


def _confirmation_code(rtype, record):
    # Код меняется, если запись изменилась после предпросмотра.
    raw = f"{rtype}:{record['id']}:{record['created_at']}:{record['updated_at']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()


def prepare_delete(conn, record_type, record_id):
    """Шаг 1 из 2: показать, что будет удалено, и выдать код подтверждения."""
    record = get_record(conn, record_type, record_id)
    rtype = record_type.strip().lower()
    return {
        "record_type": rtype,
        "id": record["id"],
        "record": record,
        "confirmation_code": _confirmation_code(rtype, record),
        "message": "Покажите запись пользователю и удаляйте только после его явного согласия.",
    }


def delete_record(conn, record_type, record_id, confirmation_code, backup_dir=None):
    """Шаг 2 из 2: удалить запись (со связанными данными) после резервной копии и её проверки."""
    record = get_record(conn, record_type, record_id)
    rtype = record_type.strip().lower()
    expected = _confirmation_code(rtype, record)
    if not isinstance(confirmation_code, str) or confirmation_code.strip().upper() != expected:
        raise ConfirmationError("Неверный или устаревший код подтверждения. Сначала вызовите предпросмотр удаления.")
    table = RECORD_TYPES[rtype][0]
    # Копия делается до транзакции: backup() внутри BEGIN IMMEDIATE того же соединения зависает.
    try:
        backup_path = backup(conn, backup_dir, label=f"before-delete-{rtype}-{record['id']}")
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"Резервная копия не создана: {exc}. Удаление не выполнено.") from None
    verify_backup_file(backup_path, table, record["id"], record["updated_at"])
    with transaction(conn):
        deleted = conn.execute(f"DELETE FROM {table} WHERE id = ?", (record["id"],)).rowcount
    if deleted != 1:
        raise NotFoundError(f"Запись {rtype} с id={record['id']} не удалена: не найдена")
    return {"deleted": True, "record_type": rtype, "id": record["id"], "backup": str(backup_path)}


def get_status(path=None):
    """Состояние базы без создания нового файла."""
    db_path = resolve_db_path(path)
    status = {
        "database_path": str(db_path),
        "database_exists": db_path.exists(),
        "sqlite_version": sqlite3.sqlite_version,
        "schema_version_expected": latest_schema_version(),
    }
    if not db_path.exists():
        status.update(ok=False, message="Файл базы не найден; он будет создан при первой записи.")
        return status
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            status["schema_version"] = schema_version(conn)
            counts = {}
            if status["schema_version"]:
                for rtype, (table, _, _) in RECORD_TYPES.items():
                    counts[rtype] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            status["record_counts"] = counts
            status["integrity"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        status.update(ok=False, message=f"Ошибка SQLite: {exc}")
        return status
    status["ok"] = status["integrity"] == "ok" and status["schema_version"] == status["schema_version_expected"]
    return status
