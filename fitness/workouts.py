"""Тренировки, упражнения и подходы."""

from . import validation as v
from .common import existing_by_request_id, row_to_dict
from .db import transaction
from .errors import NotFoundError

WORKOUT_TYPES = ("strength", "cardio", "mobility", "sport", "other")
WORKOUT_TYPE_ALIASES = {
    "силовая": "strength", "кардио": "cardio", "растяжка": "mobility",
    "мобильность": "mobility", "спорт": "sport", "другое": "other",
}


def _parse_set(raw, where):
    v.check_object(raw, {"weight_kg", "reps", "rir", "duration_sec", "distance_m", "is_warmup", "notes"}, where)
    item = {
        "weight_kg": v.parse_number(raw.get("weight_kg"), f"{where}.weight_kg", min_value=0, max_value=1000),
        "reps": v.parse_number(raw.get("reps"), f"{where}.reps", integer=True, min_value=0, max_value=1000),
        "rir": v.parse_number(raw.get("rir"), f"{where}.rir", min_value=0, max_value=10),
        "duration_sec": v.parse_number(raw.get("duration_sec"), f"{where}.duration_sec",
                                       min_value=0, min_exclusive=True, max_value=86400),
        "distance_m": v.parse_number(raw.get("distance_m"), f"{where}.distance_m",
                                     min_value=0, min_exclusive=True, max_value=500000),
        "is_warmup": v.parse_bool(raw.get("is_warmup"), f"{where}.is_warmup", default=False),
        "notes": v.parse_text(raw.get("notes"), f"{where}.notes"),
    }
    v.require_any(item, ("reps", "duration_sec", "distance_m"), where)
    return item


def _parse_exercise(raw, where):
    v.check_object(raw, {"name", "sets", "notes", "discomfort"}, where)
    sets = v.parse_list(raw.get("sets"), f"{where}.sets", max_items=100)
    return {
        "name": v.parse_text(raw.get("name"), f"{where}.name", required=True, max_len=v.MAX_SHORT_TEXT),
        "notes": v.parse_text(raw.get("notes"), f"{where}.notes"),
        "discomfort": v.parse_text(raw.get("discomfort"), f"{where}.discomfort"),
        "sets": [_parse_set(s, f"{where}.sets[{i}]") for i, s in enumerate(sets)],
    }


def _exercise_id(conn, name):
    key = v.normalize_key(name)
    row = conn.execute("SELECT id FROM exercises WHERE name_key = ?", (key,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO exercises (name, name_key) VALUES (?, ?)", (name, key)).lastrowid


def log_workout(conn, data):
    """Сохранить тренировку целиком одной транзакцией.

    data: workout_date (обяз.), title (обяз.), exercises (обяз., ≥1), workout_type, started_time,
    duration_min, notes, client_request_id.
    """
    v.check_object(data, {"workout_date", "title", "workout_type", "started_time", "duration_min",
                          "notes", "exercises", "client_request_id"}, "workout")
    workout_date = v.parse_date(data.get("workout_date"), "workout_date")
    title = v.parse_text(data.get("title"), "title", required=True, max_len=v.MAX_SHORT_TEXT)
    workout_type = v.parse_enum(data.get("workout_type"), "workout_type", WORKOUT_TYPES,
                                aliases=WORKOUT_TYPE_ALIASES) or "other"
    started_time = v.parse_time(data.get("started_time"), "started_time")
    duration = v.parse_number(data.get("duration_min"), "duration_min", min_value=0, min_exclusive=True,
                              max_value=1440)
    notes = v.parse_text(data.get("notes"), "notes")
    request_id = v.parse_request_id(data.get("client_request_id"))
    raw_exercises = v.parse_list(data.get("exercises"), "exercises", required=True, min_items=1, max_items=50)
    exercises = [_parse_exercise(e, f"exercises[{i}]") for i, e in enumerate(raw_exercises)]

    with transaction(conn):
        existing = existing_by_request_id(conn, "workouts", request_id)
        if existing:
            return {"id": existing, "duplicate": True, "record": get_workout(conn, existing)}
        workout_id = conn.execute(
            "INSERT INTO workouts (workout_date, started_time, title, workout_type, duration_min, notes,"
            " client_request_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workout_date, started_time, title, workout_type, duration, notes, request_id),
        ).lastrowid
        for position, ex in enumerate(exercises, start=1):
            we_id = conn.execute(
                "INSERT INTO workout_exercises (workout_id, exercise_id, position, notes, discomfort)"
                " VALUES (?, ?, ?, ?, ?)",
                (workout_id, _exercise_id(conn, ex["name"]), position, ex["notes"], ex["discomfort"]),
            ).lastrowid
            conn.executemany(
                "INSERT INTO exercise_sets (workout_exercise_id, set_number, weight_kg, reps, rir,"
                " duration_sec, distance_m, is_warmup, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(we_id, n, s["weight_kg"], s["reps"], s["rir"], s["duration_sec"], s["distance_m"],
                  int(s["is_warmup"]), s["notes"]) for n, s in enumerate(ex["sets"], start=1)],
            )
    return {"id": workout_id, "duplicate": False, "record": get_workout(conn, workout_id)}


def _workout_dict(conn, row):
    record = row_to_dict(row)
    exercises = []
    for ex in conn.execute(
        "SELECT we.id, we.position, e.name, we.notes, we.discomfort"
        " FROM workout_exercises we JOIN exercises e ON e.id = we.exercise_id"
        " WHERE we.workout_id = ? ORDER BY we.position",
        (row["id"],),
    ).fetchall():
        sets = []
        for s in conn.execute(
            "SELECT set_number, weight_kg, reps, rir, duration_sec, distance_m, is_warmup, notes"
            " FROM exercise_sets WHERE workout_exercise_id = ? ORDER BY set_number",
            (ex["id"],),
        ):
            item = dict(s)
            item["is_warmup"] = bool(item["is_warmup"])
            sets.append(item)
        exercises.append({"position": ex["position"], "name": ex["name"], "notes": ex["notes"],
                          "discomfort": ex["discomfort"], "sets": sets})
    record["exercises"] = exercises
    return record


def get_workout(conn, workout_id):
    row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Тренировка с id={workout_id} не найдена")
    return _workout_dict(conn, row)


DETAILS = ("full", "summary")


def _workout_brief(full):
    """Краткая форма тренировки: без подходов, с их числом и максимальным весом по упражнению."""
    brief = {k: full.get(k) for k in ("id", "workout_date", "started_time", "title", "workout_type", "duration_min")}
    brief["exercises"] = []
    for ex in full["exercises"]:
        work = [s for s in ex["sets"] if not s["is_warmup"]]
        weights = [s["weight_kg"] for s in work if s["weight_kg"] is not None]
        brief["exercises"].append({"name": ex["name"], "sets": len(ex["sets"]), "work_sets": len(work),
                                   "max_weight_kg": max(weights) if weights else None,
                                   "reps": [s["reps"] for s in work if s["reps"] is not None]})
    return brief


def get_workouts(conn, date_from, date_to, exercise=None, workout_type=None, limit=None, detail="full"):
    """Тренировки за период. limit — не больше стольких последних тренировок (остальные: truncated, total_count);
    detail='summary' — без подходов (число подходов, максимальный вес и повторения по упражнению)."""
    start, end = v.parse_date_range(date_from, date_to)
    detail = v.parse_enum(detail, "detail", DETAILS) or "full"
    if limit is not None:
        limit = v.parse_number(limit, "limit", integer=True, min_value=1, max_value=100)
    where = " FROM workouts WHERE workout_date BETWEEN ? AND ?"
    params = [start, end]
    if exercise is not None:
        name = v.parse_text(exercise, "exercise", required=True, max_len=v.MAX_SHORT_TEXT)
        where += (" AND id IN (SELECT we.workout_id FROM workout_exercises we"
                  " JOIN exercises e ON e.id = we.exercise_id WHERE e.name_key = ?)")
        params.append(v.normalize_key(name))
    if workout_type is not None:
        where += " AND workout_type = ?"
        params.append(v.parse_enum(workout_type, "workout_type", WORKOUT_TYPES, aliases=WORKOUT_TYPE_ALIASES))
    total = conn.execute("SELECT COUNT(*)" + where, params).fetchone()[0]
    if limit is not None and total > limit:
        # Последние limit тренировок периода, затем снова по возрастанию даты.
        rows = conn.execute("SELECT *" + where + " ORDER BY workout_date DESC, started_time DESC, id DESC LIMIT ?",
                            params + [limit]).fetchall()[::-1]
    else:
        rows = conn.execute("SELECT *" + where + " ORDER BY workout_date, started_time, id", params).fetchall()
    records = [_workout_dict(conn, row) for row in rows]
    if detail == "summary":
        records = [_workout_brief(r) for r in records]
    result = {"date_from": start, "date_to": end, "count": len(records), "records": records}
    if limit is not None:
        result.update(total_count=total, truncated=total > len(records), detail=detail)
        if total > len(records):
            result["hint"] = (f"Показаны последние {len(records)} из {total} тренировок периода. Сузьте период, "
                              f"запросите detail='summary' или одну тренировку через get_record(record_type='workout').")
    return result


def list_exercises(conn):
    return [dict(r) for r in conn.execute("SELECT id, name FROM exercises ORDER BY name_key")]

