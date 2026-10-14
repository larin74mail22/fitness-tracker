"""MCP-инструменты: описания (JSON Schema) и обработчики.

Каждый вызов открывает базу, выполняет одну операцию и закрывает базу.
Результат — JSON в text-блоке (и тот же объект в structuredContent).
Ошибки возвращаются как isError=true с понятным текстом, сервер при этом не падает.
"""

import json
import sqlite3
import sys
import traceback

from . import (__version__, alcohol, db, health_format, health_import, health_inbox, health_status, health_transfer,
               measurements, notes, nutrition, period_stats, records, workouts)
from .errors import (BackupError, DatabaseMissingError, MigrationPendingError, TrackerError,
                     ValidationError)
from .validation import check_object

MEASUREMENT_TYPES = ["waist", "chest", "hips", "neck", "shoulders", "biceps_left", "biceps_right",
                     "forearm_left", "forearm_right", "thigh_left", "thigh_right", "calf_left", "calf_right",
                     "body_fat_percent"]

# ---------- Общие куски схем ----------

DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
        "description": "Дата YYYY-MM-DD. Только явно названная пользователем дата."}
TIME = {"type": "string", "pattern": r"^\d{2}:\d{2}$", "description": "Местное время HH:MM (24 часа)."}
TEXT = {"type": "string", "maxLength": 2000}
SHORT = {"type": "string", "minLength": 1, "maxLength": 200}
REQUEST_ID = {"type": "string", "minLength": 1, "maxLength": 100,
              "description": "Уникальный id этой записи (например, UUID). Повтор с тем же id не создаёт дубликат."}
ID = {"type": "integer", "minimum": 1}
RECORD_TYPE = {"type": "string", "enum": list(records.RECORD_TYPES)}


def _num(minimum=None, maximum=None, description=None, integer=False):
    schema = {"type": "integer" if integer else "number"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if description:
        schema["description"] = description
    return schema


def _obj(properties, required=(), description=None):
    schema = {"type": "object", "properties": properties, "required": list(required),
              "additionalProperties": False}
    if description:
        schema["description"] = description
    return schema


PERIOD = {"date_from": dict(DATE, description="Начало периода включительно, YYYY-MM-DD."),
          "date_to": dict(DATE, description="Конец периода включительно, YYYY-MM-DD.")}

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


# ---------- Контекст: где лежит база ----------

def _db_path():
    return db.resolve_db_path()


def _backup_dir():
    return _db_path().parent / "backups"


def _photos_dir():
    return _db_path().parent / "photos"


def _with_db(func):
    conn = db.open_database(_db_path())
    try:
        return func(conn)
    finally:
        conn.close()


def _args(args, allowed):
    return check_object(args, set(allowed), "arguments")


# ---------- Обработчики ----------

# Предел длины списков в ответах: большие периоды не должны переполнять контекст ChatGPT.
MAX_MEALS, MAX_ALCOHOL_ENTRIES = 60, 100

def _status(args):
    _args(args, ())
    status = records.get_status(_db_path())
    status["database_ok"] = status.pop("ok")   # "ok" в ответе означает успешный вызов инструмента
    status["server_version"] = __version__
    status["tools"] = [t["name"] for t in TOOLS]
    return status


def _log_measurement(args):
    return _with_db(lambda c: measurements.log_measurement(c, args))


def _limit(args, default, maximum):
    value = args.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError("limit", f"целое 1…{maximum}")
    return value


def _cap(result, key, limit, what="записей"):
    """Оставить в result[key] не больше limit последних элементов: большие периоды не должны переполнять ответ."""
    items = result[key]
    result[f"{key}_total"] = len(items)
    result[f"{key}_truncated"] = len(items) > limit
    if len(items) > limit:
        result[key] = items[-limit:]
        result["hint"] = (f"Показаны последние {limit} из {len(items)} {what}. Сузьте период"
                          f"{' или увеличьте limit' if key == 'records' else ''}; итоги за период — get_period_stats.")
    return result


def _get_measurements(args):
    _args(args, ("date_from", "date_to", "measurement_type", "limit"))
    limit = _limit(args, 100, 500)
    result = _with_db(lambda c: measurements.get_measurements(c, args.get("date_from"), args.get("date_to"),
                                                              args.get("measurement_type")))
    _cap(result, "records", limit)
    result["count"] = len(result["records"])
    return result


def _add_photo(args):
    return _with_db(lambda c: measurements.add_photo(c, args, photos_dir=_photos_dir()))


def _log_workout(args):
    return _with_db(lambda c: workouts.log_workout(c, args))


def _get_workouts(args):
    _args(args, ("date_from", "date_to", "exercise", "workout_type", "limit", "detail"))
    limit = _limit(args, 20, 100)
    return _with_db(lambda c: workouts.get_workouts(c, args.get("date_from"), args.get("date_to"),
                                                    exercise=args.get("exercise"),
                                                    workout_type=args.get("workout_type"),
                                                    limit=limit, detail=args.get("detail", "full")))


def _list_exercises(args):
    _args(args, ())
    return _with_db(lambda c: {"exercises": workouts.list_exercises(c)})


def _log_meal(args):
    return _with_db(lambda c: nutrition.log_meal(c, args))


def _nutrition_summary(args):
    _args(args, ("date_from", "date_to", "include_meals"))
    include = args.get("include_meals", False)
    if not isinstance(include, bool):
        raise ValidationError("include_meals", "ожидается true или false")
    result = _with_db(lambda c: nutrition.get_nutrition_summary(c, args.get("date_from"), args.get("date_to"),
                                                                include_meals=include))
    if include:
        _cap(result, "meals", MAX_MEALS, what="приёмов пищи")
    return result


def _log_alcohol(args):
    return _with_db(lambda c: alcohol.log_alcohol(c, args))


def _alcohol_summary(args):
    _args(args, ("date_from", "date_to"))
    result = _with_db(lambda c: alcohol.get_alcohol_summary(c, args.get("date_from"), args.get("date_to")))
    return _cap(result, "entries", MAX_ALCOHOL_ENTRIES, what="записей (итоги посчитаны по всем)")


def _period_stats(args):
    _args(args, ("date_from", "date_to"))
    return _with_db(lambda c: period_stats.period_stats(c, args.get("date_from"), args.get("date_to")))


def _add_note(args):
    return _with_db(lambda c: notes.add_note(c, args))


def _search(args):
    _args(args, ("record_types", "date_from", "date_to", "query", "note_category", "limit"))
    return _with_db(lambda c: records.search_records(
        c, record_types=args.get("record_types"), date_from=args.get("date_from"), date_to=args.get("date_to"),
        query=args.get("query"), note_category=args.get("note_category"), limit=args.get("limit", 50)))


def _get_record(args):
    _args(args, ("record_type", "id"))
    return _with_db(lambda c: {"record_type": args.get("record_type"),
                               "record": records.get_record(c, args.get("record_type"), args.get("id"))})


def _prepare_delete(args):
    _args(args, ("record_type", "id"))
    return _with_db(lambda c: records.prepare_delete(c, args.get("record_type"), args.get("id")))


def _delete(args):
    _args(args, ("record_type", "id", "confirmation_code"))
    return _with_db(lambda c: records.delete_record(c, args.get("record_type"), args.get("id"),
                                                    args.get("confirmation_code"), backup_dir=_backup_dir()))


def _backup(args):
    _args(args, ())
    if not _db_path().exists():
        raise TrackerError("Базы ещё нет — копировать нечего.")
    path = _with_db(lambda c: db.backup(c, _backup_dir()))
    return {"backup": str(path)}


# ---------- Импорт Health Connect ----------

def _uploads_dir():
    return _db_path().parent / "health_uploads"


def _require_db():
    """Импорт базу не создаёт: без существующей базы не принимаем и части файла."""
    if not _db_path().is_file():
        raise DatabaseMissingError(f"База данных не найдена: {_db_path()}. Импорт базу не создаёт.")


def _flag(args, name):
    value = args.get(name, False)
    if not isinstance(value, bool):
        raise ValidationError(name, "ожидается true или false")
    return value


def _import_report(report):
    totals = {c: sum(report["counts"][k][c] for k in health_format.RECORD_TYPES) for c in health_import.COUNTERS}
    return {
        "status": report["status"], "import_id": report["batch_id"], "dry_run": report["dry_run"],
        "file": report["file"], "sha256": report["sha256"], "phone_id": report["phone_id"],
        "sequence": report["sequence"], "exported_at": report["exported_at"], "data_range": report["data_range"],
        "counts": report["counts"], "totals": totals,
        "skipped": report["not_applied"],
        "unmatched_deletions": report["unmatched_deletions"], "out_of_scope_deletions": report["out_of_scope_deletions"],
        "warnings": report["warnings"], "errors": [],
        "gap": report.get("gap"), "period": report.get("period"),
    }


def _import_health_export(args, downloader=None):
    _args(args, ("file", "upload_id", "dry_run", "allow_gaps", "allow_older"))
    dry_run, allow_gaps, allow_older = (_flag(args, n) for n in ("dry_run", "allow_gaps", "allow_older"))
    has_file, has_upload = args.get("file") is not None, args.get("upload_id") is not None
    if has_file == has_upload:
        raise ValidationError("file", "передайте ровно одно: file (файл из сообщения) или upload_id (передача частями)")
    _require_db()
    if has_file:
        data, name = health_transfer.fetch_file_param(args["file"], downloader)
    else:
        data, name = health_transfer.assemble(_uploads_dir(), args["upload_id"])
    report = health_import.import_bytes(data, name, _db_path(), dry_run=dry_run, allow_gaps=allow_gaps,
                                        allow_older=allow_older)
    if has_upload and not dry_run:
        health_transfer.discard(_uploads_dir(), args["upload_id"])     # файл в базе, части больше не нужны
    return _import_report(report)


def _start_health_upload(args):
    _args(args, ("total_bytes", "sha256", "chunk_count", "encoding", "file_name"))
    _require_db()
    return health_transfer.start_upload(_uploads_dir(), args.get("total_bytes"), args.get("sha256"),
                                        args.get("chunk_count"), args.get("encoding", "utf8"), args.get("file_name"))


def _upload_health_chunk(args):
    _args(args, ("upload_id", "index", "data", "chunk_sha256"))
    return health_transfer.put_chunk(_uploads_dir(), args.get("upload_id"), args.get("index"), args.get("data"),
                                     args.get("chunk_sha256"))


def _health_imports(args):
    _args(args, ("limit",))
    limit = args.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("limit", "целое 1…100")
    _require_db()
    conn = db.connect(_db_path(), create=False)
    try:
        if db.schema_version(conn) < 2:
            raise MigrationPendingError("В базе ещё нет таблиц Health Connect: нужна команда migrate_db.py.")
        batches = [dict(r) for r in conn.execute(
            "SELECT id AS import_id, file_name, phone_id, sequence, exported_at, imported_at, status, error "
            "FROM import_batches ORDER BY id DESC LIMIT ?", (limit,))]
        for b in batches:
            b["counts"] = {r["record_type"]: {c: r[c] for c in health_import.COUNTERS} for r in conn.execute(
                "SELECT * FROM import_batch_counts WHERE batch_id = ?", (b["import_id"],))}
            if b["error"] and len(b["error"]) > 1000:
                b["error"] = b["error"][:1000] + "…"
        phones = [dict(r) for r in conn.execute(
            "SELECT phone_id, MAX(sequence) AS last_sequence, MAX(exported_at) AS last_exported_at "
            "FROM import_batches WHERE status = 'ok' GROUP BY phone_id ORDER BY phone_id")]
    finally:
        conn.close()
    return {"imports": batches, "phones": phones}


def _health_sync_status(args):
    _args(args, ())
    return health_status.sync_status(_db_path())


def _health_summary(args):
    _args(args, ("date_from", "date_to", "days", "include_days", "compare"))
    include_days = args.get("include_days", True)
    compare = args.get("compare", True)
    for name, value in (("include_days", include_days), ("compare", compare)):
        if not isinstance(value, bool):
            raise ValidationError(name, "ожидается true или false")
    return health_status.health_summary(_db_path(), args.get("date_from"), args.get("date_to"), args.get("days"),
                                        include_days=include_days, compare=compare)


def _daily_activity(args):
    _args(args, ("date_from", "date_to"))
    return health_status.daily_activity(_db_path(), args.get("date_from"), args.get("date_to"))


def _import_health_inbox(args):
    _args(args, ("dry_run",))
    dry_run = _flag(args, "dry_run")
    _require_db()
    run = health_inbox.process_inbox(_db_path(), dry_run=dry_run)
    if run["error"] and run["error"]["code"] == health_inbox.InboxLocked.code:
        raise health_inbox.InboxLocked(run["error"]["message"])
    return {"run": run, "text": health_inbox.describe_run(run)}


# ---------- Описания инструментов ----------

MEASUREMENT_ITEM = _obj({
    "type": {"type": "string", "enum": MEASUREMENT_TYPES,
             "description": "Вид замера. Обхваты в см, body_fat_percent в %."},
    "value": _num(0, description="Значение в см (или в % для body_fat_percent)."),
}, required=("type", "value"))

SET_ITEM = _obj({
    "weight_kg": _num(0, 1000, "Вес снаряда в кг (0 — собственный вес)."),
    "reps": _num(0, 1000, "Повторения.", integer=True),
    "rir": _num(0, 10, "Повторений в запасе (RIR)."),
    "duration_sec": _num(0, 86400, "Длительность подхода в секундах."),
    "distance_m": _num(0, 500000, "Дистанция в метрах."),
    "is_warmup": {"type": "boolean", "description": "Разминочный подход. По умолчанию false."},
    "notes": TEXT,
}, description="Подход. Нужно хотя бы одно из: reps, duration_sec, distance_m.")

EXERCISE_ITEM = _obj({
    "name": dict(SHORT, description="Название упражнения, например «Жим лёжа»."),
    "sets": {"type": "array", "items": SET_ITEM, "maxItems": 100, "description": "Подходы по порядку."},
    "notes": TEXT,
    "discomfort": dict(TEXT, description="Дискомфорт или боль при выполнении, со слов пользователя."),
}, required=("name",))

MEAL_ITEM = _obj({
    "name": dict(SHORT, description="Продукт или блюдо."),
    "quantity": _num(0, 100000, "Количество; передаётся только вместе с unit."),
    "unit": {"type": "string", "enum": ["g", "ml", "pcs", "serving"],
             "description": "g — граммы, ml — миллилитры, pcs — штуки, serving — порции."},
    "kcal": _num(0, 10000, "Калории."),
    "protein_g": _num(0, 1000, "Белки, г."),
    "fat_g": _num(0, 1000, "Жиры, г."),
    "carbs_g": _num(0, 2000, "Углеводы, г."),
    "nutrition_source": {"type": "string", "enum": ["label", "database", "recipe", "user", "estimate"],
                         "description": "Откуда КБЖУ: label — этикетка, database — справочник, recipe — расчёт "
                                        "по рецепту, user — назвал пользователь, estimate — твоя оценка. "
                                        "Обязательно, если передано хоть одно из kcal/protein_g/fat_g/carbs_g."},
    "is_estimate": {"type": "boolean",
                    "description": "Приблизительные ли значения. По умолчанию true; false только для точных данных."},
}, required=("name",), description="Продукт. Неизвестные значения не передавай.")

TOOLS = [
    {
        "name": "get_tracker_status",
        "title": "Состояние трекера",
        "description": "Проверить базу: существует ли файл, версия схемы, целостность, число записей по типам, "
                       "версия сервера. Ничего не изменяет.",
        "inputSchema": _obj({}),
        "annotations": READ_ONLY,
        "handler": _status,
    },
    {
        "name": "log_measurement",
        "title": "Записать вес и замеры",
        "description": "Записать вес и/или обхваты тела за одну дату. Нужно measured_on и хотя бы weight_kg или "
                       "measurements. Результат: id записи и сохранённая запись; duplicate=true, если запись с "
                       "этим client_request_id уже была.",
        "inputSchema": _obj({
            "measured_on": dict(DATE, description="Дата измерения YYYY-MM-DD."),
            "measured_time": TIME,
            "weight_kg": _num(20, 400, "Вес тела в кг. Фунты переведи в кг только с согласия пользователя."),
            "measurements": {"type": "array", "items": MEASUREMENT_ITEM, "maxItems": 30},
            "notes": TEXT,
            "client_request_id": REQUEST_ID,
        }, required=("measured_on",)),
        "annotations": WRITE,
        "handler": _log_measurement,
    },
    {
        "name": "get_measurements",
        "title": "Замеры за период",
        "description": "Получить вес и замеры за период (включительно), по возрастанию даты. "
                       "measurement_type='weight' или код замера оставляет только записи с этим значением. "
                       "Не больше limit последних записей (по умолчанию 100); records_truncated=true — есть ещё.",
        "inputSchema": _obj(dict(PERIOD, measurement_type={"type": "string", "enum": ["weight"] + MEASUREMENT_TYPES},
                                 limit=_num(1, 500, "Максимум записей, по умолчанию 100.", integer=True)),
                            required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _get_measurements,
    },
    {
        "name": "add_photo",
        "title": "Привязать фото",
        "description": "Записать фото прогресса. Файл уже должен лежать в папке data/photos проекта; "
                       "file_path — путь относительно неё. session_id — id замера из log_measurement (необязательно).",
        "inputSchema": _obj({
            "taken_on": dict(DATE, description="Дата фото YYYY-MM-DD."),
            "file_path": dict(SHORT, description="Путь внутри data/photos, например 2026/10-10-front.jpg."),
            "session_id": ID,
            "view": {"type": "string", "enum": ["front", "side", "back", "other"]},
            "notes": TEXT,
        }, required=("taken_on", "file_path")),
        "annotations": WRITE,
        "handler": _add_photo,
    },
    {
        "name": "log_workout",
        "title": "Записать тренировку",
        "description": "Сохранить тренировку с упражнениями и подходами одной операцией (всё или ничего). "
                       "Подходы нумеруются по порядку. Результат: id и сохранённая тренировка.",
        "inputSchema": _obj({
            "workout_date": dict(DATE, description="Дата тренировки YYYY-MM-DD."),
            "title": dict(SHORT, description="Название, например «Верх тела»."),
            "workout_type": {"type": "string", "enum": list(workouts.WORKOUT_TYPES),
                             "description": "strength — силовая, cardio, mobility — растяжка/мобильность, "
                                            "sport — игровой спорт, other. По умолчанию other."},
            "started_time": TIME,
            "duration_min": _num(0, 1440, "Длительность тренировки в минутах."),
            "notes": TEXT,
            "exercises": {"type": "array", "items": EXERCISE_ITEM, "minItems": 1, "maxItems": 50},
            "client_request_id": REQUEST_ID,
        }, required=("workout_date", "title", "exercises")),
        "annotations": WRITE,
        "handler": _log_workout,
    },
    {
        "name": "get_workouts",
        "title": "История тренировок",
        "description": "Тренировки за период (для одного дня date_from = date_to) с упражнениями и подходами: вес, "
                       "повторения, RIR, длительность, дистанция, разминка, заметки и дискомфорт — всё, что было "
                       "сохранено. exercise — только тренировки с этим упражнением (без учёта регистра и ё/е). "
                       "Не больше limit последних тренировок (по умолчанию 20); truncated=true и total_count — если "
                       "их больше. detail='summary' — без подходов (их число, максимальный вес, повторения). "
                       "Исходный текст сообщения пользователя о тренировке не хранится: есть только сохранённые поля "
                       "и notes; не выдавай их за дословный текст.",
        "inputSchema": _obj(dict(PERIOD, exercise=SHORT,
                                 workout_type={"type": "string", "enum": list(workouts.WORKOUT_TYPES)},
                                 limit=_num(1, 100, "Максимум тренировок, по умолчанию 20.", integer=True),
                                 detail={"type": "string", "enum": list(workouts.DETAILS),
                                         "description": "full (по умолчанию) — с подходами; summary — кратко."}),
                            required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _get_workouts,
    },
    {
        "name": "list_exercises",
        "title": "Список упражнений",
        "description": "Все названия упражнений, которые уже встречались. Используй, чтобы называть "
                       "упражнения одинаково.",
        "inputSchema": _obj({}),
        "annotations": READ_ONLY,
        "handler": _list_exercises,
    },
    {
        "name": "log_meal",
        "title": "Записать приём пищи",
        "description": "Записать приём пищи из одного или нескольких продуктов. Не выдумывай КБЖУ: "
                       "если оцениваешь сам — nutrition_source='estimate'.",
        "inputSchema": _obj({
            "eaten_on": dict(DATE, description="Дата приёма пищи YYYY-MM-DD."),
            "eaten_time": TIME,
            "meal_type": {"type": "string", "enum": list(nutrition.MEAL_TYPES)},
            "notes": TEXT,
            "items": {"type": "array", "items": MEAL_ITEM, "minItems": 1, "maxItems": 50},
            "client_request_id": REQUEST_ID,
        }, required=("eaten_on", "items")),
        "annotations": WRITE,
        "handler": _log_meal,
    },
    {
        "name": "get_nutrition_summary",
        "title": "Сводка питания",
        "description": "КБЖУ по дням за период (для одного дня date_from = date_to). Суммы — только по известным "
                       "значениям; items_without_kcal и totals_complete показывают неполноту, items_estimated — "
                       "сколько значений приблизительные. include_meals=true добавляет список приёмов пищи с "
                       "продуктами (не больше 60 последних; meals_truncated=true — есть ещё).",
        "inputSchema": _obj(dict(PERIOD, include_meals={"type": "boolean"}), required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _nutrition_summary,
    },
    {
        "name": "log_alcohol",
        "title": "Записать алкоголь",
        "description": "Записать напиток. Граммы спирта считаются, только если известны volume_ml и abv_percent. "
                       "Без kcal калории оцениваются только по спирту (без сахара) и помечаются как оценка.",
        "inputSchema": _obj({
            "consumed_on": dict(DATE, description="Дата YYYY-MM-DD."),
            "consumed_time": TIME,
            "drink": dict(SHORT, description="Напиток, например «Пиво светлое»."),
            "volume_ml": _num(0, 10000, "Объём в мл."),
            "abv_percent": _num(0, 100, "Крепость, % об."),
            "kcal": _num(0, 10000, "Калории, если известны."),
            "kcal_source": {"type": "string", "enum": ["user", "label"],
                            "description": "Обязательно вместе с kcal: user — со слов пользователя, label — этикетка."},
            "notes": TEXT,
            "client_request_id": REQUEST_ID,
        }, required=("consumed_on", "drink")),
        "annotations": WRITE,
        "handler": _log_alcohol,
    },
    {
        "name": "get_alcohol_summary",
        "title": "Сводка по алкоголю",
        "description": "Итоги за период (дни с алкоголем, граммы спирта, калории, сколько записей без данных) и "
                       "записи (не больше 100 последних; итоги — по всем).",
        "inputSchema": _obj(dict(PERIOD), required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _alcohol_summary,
    },
    {
        "name": "get_period_stats",
        "title": "Статистика за период",
        "description": "Компактные итоги за период по всем разделам без списков записей: тренировки (число, типы, "
                       "длительность, по упражнениям: тренировок, рабочих подходов, максимальный вес, повторения, "
                       "объём, первая и последняя дата; до 25 упражнений), питание (средние КБЖУ по дням с "
                       "калориями, полнота), вес и замеры (первое, последнее, изменение), алкоголь, заметки по "
                       "категориям, шаги, пульс и сон. Для вопросов «как прошёл месяц» — вместо выгрузки всех "
                       "записей. Ничего не изменяет.",
        "inputSchema": _obj(dict(PERIOD), required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _period_stats,
    },
    {
        "name": "add_note",
        "title": "Добавить заметку",
        "description": "Заметка с категорией (сон, самочувствие, травма, …) и при необходимости "
                       "структурированными полями, например {key: 'сон', value: 7.5, unit: 'ч'}.",
        "inputSchema": _obj({
            "note_date": dict(DATE, description="Дата YYYY-MM-DD."),
            "category": {"type": "string", "minLength": 1, "maxLength": 50},
            "text": dict(TEXT, minLength=1),
            "fields": {"type": "array", "maxItems": 30, "items": _obj({
                "key": {"type": "string", "minLength": 1, "maxLength": 50},
                "value": {"type": ["number", "string"]},
                "unit": {"type": "string", "maxLength": 20},
            }, required=("key", "value"))},
            "client_request_id": REQUEST_ID,
        }, required=("note_date", "category", "text")),
        "annotations": WRITE,
        "handler": _add_note,
    },
    {
        "name": "search_records",
        "title": "Поиск записей",
        "description": "Найти записи по типам, периоду, словам (без учёта регистра) и категории заметок. "
                       "Нужен хотя бы один фильтр. Возвращает краткий список (тип, id, дата, описание); "
                       "подробности — через get_record.",
        "inputSchema": _obj({
            "record_types": {"type": "array", "items": {"type": "string", "enum": list(records.SEARCH_TYPES)},
                             "maxItems": 5},
            "date_from": PERIOD["date_from"],
            "date_to": PERIOD["date_to"],
            "query": SHORT,
            "note_category": {"type": "string", "minLength": 1, "maxLength": 50},
            "limit": _num(1, 200, "Максимум результатов, по умолчанию 50.", integer=True),
        }),
        "annotations": READ_ONLY,
        "handler": _search,
    },
    {
        "name": "get_record",
        "title": "Запись целиком",
        "description": "Получить одну запись по типу и id со всеми связанными данными.",
        "inputSchema": _obj({"record_type": RECORD_TYPE, "id": ID}, required=("record_type", "id")),
        "annotations": READ_ONLY,
        "handler": _get_record,
    },
    {
        "name": "prepare_delete_record",
        "title": "Удаление: шаг 1",
        "description": "Шаг 1 из 2. Показывает запись, которая будет удалена, и выдаёт confirmation_code. "
                       "Ничего не удаляет. Покажи запись пользователю и спроси явное согласие.",
        "inputSchema": _obj({"record_type": RECORD_TYPE, "id": ID}, required=("record_type", "id")),
        "annotations": READ_ONLY,
        "handler": _prepare_delete,
    },
    {
        "name": "delete_record",
        "title": "Удаление: шаг 2",
        "description": "Шаг 2 из 2. Удаляет запись со связанными данными. Вызывать только после явного согласия "
                       "пользователя, с кодом из prepare_delete_record. Перед удалением автоматически делается "
                       "резервная копия базы.",
        "inputSchema": _obj({"record_type": RECORD_TYPE, "id": ID,
                             "confirmation_code": {"type": "string", "minLength": 1, "maxLength": 20}},
                            required=("record_type", "id", "confirmation_code")),
        "annotations": DESTRUCTIVE,
        "handler": _delete,
    },
    {
        "name": "backup_database",
        "title": "Резервная копия",
        "description": "Сделать резервную копию базы в новый файл data/backups/fitness-ДАТА-ВРЕМЯ.db. "
                       "Старые копии не перезаписываются.",
        "inputSchema": _obj({}),
        "annotations": WRITE,
        "handler": _backup,
    },
    {
        "name": "import_health_export",
        "title": "Импорт выгрузки Health Connect",
        "description": "Импортировать файл выгрузки Health Connect (JSON fitness-health-export v1 из приложения "
                       "FITNESS Health Export: шаги, пульс, сон) в локальную базу. Основной способ: пользователь "
                       "прикрепил файл к сообщению — передай его в параметре file, содержимое пересказывать не нужно. "
                       "Если параметр файла недоступен, передай файл частями (start_health_upload, "
                       "upload_health_chunk) и вызови этот инструмент с upload_id. Файл проверяется целиком и пишется "
                       "одной транзакцией: при любой ошибке в базе ничего не меняется. Повтор того же файла и "
                       "перекрывающиеся периоды дубликатов не создают. dry_run=true — проверка без записи. "
                       "Пропуск номеров файлов принимается сам, если период файла перекрывает данные в базе (поле gap "
                       "в ответе). Ошибка import_order: пропущенные файлы нужны (order_problem=gap, есть "
                       "covered_until и period_start — непокрытый промежуток) или файл старше принятого — объясни "
                       "пользователю и повтори с allow_gaps=true или allow_older=true только с его согласия. "
                       "Результат: status "
                       "(imported, already_imported, dry_run), import_id, phone_id, sequence, data_range, counts по "
                       "типам (inserted, updated, unchanged, deleted, rejected), totals, skipped, warnings.",
        "inputSchema": _obj({
            "file": _obj({
                "download_url": {"type": "string", "description": "Ссылка на файл, её заполняет ChatGPT."},
                "file_id": {"type": "string", "description": "Идентификатор файла, его заполняет ChatGPT."},
                "mime_type": {"type": "string"},
                "file_name": {"type": "string"},
            }, required=("download_url", "file_id"), description="Файл, прикреплённый пользователем к сообщению."),
            "upload_id": {"type": "string", "pattern": "^[0-9a-f]{32}$",
                          "description": "Идентификатор завершённой передачи частями из start_health_upload."},
            "dry_run": {"type": "boolean", "description": "Только проверить и посчитать, ничего не записывать. "
                                                          "По умолчанию false."},
            "allow_gaps": {"type": "boolean", "description": "Разрешить пропуск номеров файлов. Только с согласия "
                                                             "пользователя после ошибки import_order."},
            "allow_older": {"type": "boolean", "description": "Принять пропущенный ранее файл с номером меньше "
                                                              "последнего принятого; более новые данные не "
                                                              "затираются. Только с согласия пользователя."},
        }),
        "annotations": WRITE,
        "_meta": {"openai/fileParams": ["file"]},
        "handler": _import_health_export,
    },
    {
        "name": "start_health_upload",
        "title": "Передача файла частями: начало",
        "description": "Запасной способ, если файл нельзя передать параметром file в import_health_export. "
                       "Годится только для небольших файлов. Укажи размер файла в байтах, SHA-256 всего файла и число "
                       "частей. encoding: utf8 — части являются кусками текста файла подряд (рекомендуется), base64 — "
                       "куски байтов в base64. Затем передай каждую часть через upload_health_chunk и вызови "
                       "import_health_export с upload_id. Незавершённая передача импорт не запускает.",
        "inputSchema": _obj({
            "total_bytes": _num(1, health_format.MAX_FILE_BYTES, "Размер всего файла в байтах (UTF-8).", integer=True),
            "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$", "description": "SHA-256 всего файла."},
            "chunk_count": _num(1, health_transfer.MAX_CHUNKS, "Число частей.", integer=True),
            "encoding": {"type": "string", "enum": list(health_transfer.ENCODINGS)},
            "file_name": {"type": "string", "minLength": 1, "maxLength": 255},
        }, required=("total_bytes", "sha256", "chunk_count")),
        "annotations": WRITE,
        "handler": _start_health_upload,
    },
    {
        "name": "upload_health_chunk",
        "title": "Передача файла частями: часть",
        "description": "Передать одну часть файла. index от 0; части можно передавать в любом порядке, повтор той "
                       "же части безопасен. chunk_sha256 (необязательно) — SHA-256 байтов этой части для проверки. "
                       "Результат показывает, сколько частей получено и завершена ли передача.",
        "inputSchema": _obj({
            "upload_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "index": _num(0, health_transfer.MAX_CHUNKS - 1, "Номер части от 0.", integer=True),
            "data": {"type": "string", "minLength": 1, "maxLength": health_transfer.MAX_CHUNK_CHARS},
            "chunk_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
        }, required=("upload_id", "index", "data")),
        "annotations": WRITE,
        "handler": _upload_health_chunk,
    },
    {
        "name": "get_health_imports",
        "title": "Журнал импортов Health Connect",
        "description": "Последние импорты выгрузок Health Connect (успешные и неудачные) со счётчиками и ошибками, "
                       "а также последний принятый номер файла по каждому телефону. Ничего не изменяет.",
        "inputSchema": _obj({"limit": _num(1, 100, "Сколько последних импортов, по умолчанию 10.", integer=True)}),
        "annotations": READ_ONLY,
        "handler": _health_imports,
    },
    {
        "name": "get_health_summary",
        "title": "Шаги, пульс и сон за период",
        "description": "Сводка Health Connect по фактическим данным базы: шаги по дням (сумма, среднее, максимум), "
                       "пульс (минимум, среднее, максимум, число измерений по дням), сон по датам пробуждения "
                       "(в постели, во сне, глубокий, лёгкий, быстрый, бодрствование, минуты), сравнение с предыдущим "
                       "периодом той же длины (change). Период: date_from и date_to (YYYY-MM-DD, местные даты) или "
                       "days — последние N дней по сегодняшнюю местную дату компьютера включительно (days=1 — "
                       "сегодня). Дни без данных в списки не попадают; data_available показывает, до какой даты "
                       "есть данные (после неё — ещё не было импорта). Для итогов по каждому дню (шаги за день, "
                       "дни без данных как null) удобнее get_daily_activity. Ничего не изменяет.",
        "inputSchema": _obj({
            "date_from": PERIOD["date_from"], "date_to": PERIOD["date_to"],
            "days": _num(1, health_status.MAX_DAYS, "Последние N дней по сегодня включительно (вместо дат).",
                         integer=True),
            "include_days": {"type": "boolean", "description": "Добавить данные по дням. По умолчанию true."},
            "compare": {"type": "boolean", "description": "Сравнить с предыдущим периодом. По умолчанию true."},
        }),
        "annotations": READ_ONLY,
        "handler": _health_summary,
    },
    {
        "name": "get_daily_activity",
        "title": "Активность по дням: шаги, сон, пульс",
        "description": "Главный инструмент для суточных итогов Health Connect: каждый календарный день периода "
                       "отдельной строкой — steps (шаги за день), sleep (в постели, во сне, стадии, минуты; по дате "
                       "пробуждения), heart_rate (минимум, среднее, максимум, число измерений). Используй его для "
                       "расчёта активности и расхода калорий по дням. Если данных за день нет, значение null, а "
                       "*_status объясняет почему (not_imported_yet — ещё не было импорта, no_records — записей нет); "
                       "null нельзя считать нулём. Даты YYYY-MM-DD, местные, включительно, период до "
                       f"{health_status.MAX_ACTIVITY_DAYS} дней (для более длинного — несколько вызовов или "
                       "get_health_summary). Ничего не изменяет.",
        "inputSchema": _obj({"date_from": PERIOD["date_from"], "date_to": PERIOD["date_to"]},
                            required=("date_from", "date_to")),
        "annotations": READ_ONLY,
        "handler": _daily_activity,
    },
    {
        "name": "get_health_sync_status",
        "title": "Состояние синхронизации Health Connect",
        "description": "Когда был последний успешный импорт, сколько файлов импортировано и сколько попыток не "
                       "удалось (с текстом ошибок), до какой даты и времени есть шаги, пульс и сон, состояние "
                       "входящей папки автоматического импорта (ждут обработки, ошибки, отчёт последнего запуска) и "
                       "список того, что требует внимания (attention). Время в UTC. Ничего не изменяет.",
        "inputSchema": _obj({}),
        "annotations": READ_ONLY,
        "handler": _health_sync_status,
    },
    {
        "name": "import_health_inbox",
        "title": "Импорт файлов из входящей папки",
        "description": "Импортировать новые файлы выгрузки Health Connect, которые уже лежат во входящей папке "
                       "компьютера (data\\health_inbox; новые файлы из папки Syncthing data\\health_syncthing сначала "
                       "копируются туда, сама папка Syncthing не меняется). Тот же импортёр, "
                       "что у import_health_export: проверка, одна транзакция на файл, без дубликатов. Импортированные "
                       "файлы переносятся в health_processed, неверные — в health_failed, файлы с пропуском номеров "
                       "остаются ждать. Путей не принимает. dry_run=true — проверка без записи и без перемещения.",
        "inputSchema": _obj({"dry_run": {"type": "boolean", "description": "Только проверить. По умолчанию false."}}),
        "annotations": WRITE,
        "handler": _import_health_inbox,
    },
]

_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def list_tools():
    return [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


def _log(message):
    print(f"[fitness-tracker] {message}", file=sys.stderr, flush=True)


def _text_result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _json_result(payload, is_error):
    text = json.dumps(payload, ensure_ascii=False, default=str)
    result = _text_result(text, is_error)
    result["structuredContent"] = json.loads(text)
    return result


def call_tool(name, arguments):
    """Выполнить инструмент. Всегда возвращает результат MCP; исключения наружу не выходят."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return _text_result("Unknown tool", True)
    if not isinstance(arguments, dict):
        return _json_result({"ok": False, "error": "validation_error",
                             "message": "arguments: ожидается объект"}, True)
    try:
        payload = tool["handler"](arguments)
    except TrackerError as exc:
        _log(f"{name}: {exc.code}")
        error = exc.to_dict()
        error["ok"] = False
        if isinstance(exc, ValidationError):
            error["hint"] = "Ничего не сохранено. Уточните значение у пользователя и повторите вызов."
        elif name == "import_health_export":
            error["hint"] = "Импорт не выполнен, данные в базе не изменены."
            if getattr(exc, "journal_batch_id", None):
                error["journal_import_id"] = exc.journal_batch_id
                error["hint"] += " Неудачная попытка записана в журнал импортов."
        elif isinstance(exc, BackupError):
            error["hint"] = ("Запись НЕ удалена, данные не изменены. Сообщите пользователю о сбое резервной копии; "
                             "повторять удаление стоит только после устранения причины (например, места на диске).")
        return _json_result(error, True)
    except sqlite3.Error as exc:
        _log(f"{name}: sqlite error {exc!r}\n{traceback.format_exc()}")
        return _json_result({"ok": False, "error": "database_error",
                             "message": f"Ошибка базы данных: {exc}. Запись могла не сохраниться — проверьте через поиск, прежде чем повторять."}, True)
    except Exception as exc:
        _log(f"{name}: unexpected error {exc!r}\n{traceback.format_exc()}")
        return _json_result({"ok": False, "error": "internal_error",
                             "message": "Внутренняя ошибка сервера. Запись могла не сохраниться — проверьте через поиск, прежде чем повторять."}, True)

    _log(f"{name}: ok")
    body = {"ok": True}
    body.update(payload)
    return _json_result(body, False)

