"""Агрегированная статистика за период по всем разделам — компактно, без списков записей. Только чтение.

Нужна, чтобы ChatGPT мог ответить «как прошёл месяц/квартал» без выгрузки всей базы в контекст; подробности по
отдельным дням и записям — через get_workouts, get_nutrition_summary, get_measurements, get_health_summary.
"""

import sqlite3

from . import alcohol, nutrition
from . import validation as v
from .common import round_or_none

TOP_EXERCISES = 25


def _workouts(conn, start, end):
    row = conn.execute(
        "SELECT COUNT(*) AS workouts, COUNT(DISTINCT workout_date) AS days, SUM(duration_min) AS duration_min,"
        " SUM(duration_min IS NULL) AS without_duration FROM workouts WHERE workout_date BETWEEN ? AND ?",
        (start, end)).fetchone()
    by_type = {r["workout_type"]: r["n"] for r in conn.execute(
        "SELECT workout_type, COUNT(*) AS n FROM workouts WHERE workout_date BETWEEN ? AND ?"
        " GROUP BY workout_type ORDER BY n DESC", (start, end))}
    exercise_sql = (
        " FROM workouts w JOIN workout_exercises we ON we.workout_id = w.id"
        " JOIN exercises e ON e.id = we.exercise_id LEFT JOIN exercise_sets s ON s.workout_exercise_id = we.id"
        " WHERE w.workout_date BETWEEN ? AND ?")
    total_exercises = conn.execute("SELECT COUNT(DISTINCT e.id)" + exercise_sql, (start, end)).fetchone()[0]
    exercises = [{
        "name": r["name"], "sessions": r["sessions"], "work_sets": r["work_sets"] or 0,
        "max_weight_kg": r["max_weight"], "total_reps": r["total_reps"],
        "volume_kg": round_or_none(r["volume"]), "first_date": r["first_date"], "last_date": r["last_date"],
    } for r in conn.execute(
        "SELECT e.name, COUNT(DISTINCT w.id) AS sessions,"
        " SUM(s.id IS NOT NULL AND s.is_warmup = 0) AS work_sets,"
        " MAX(CASE WHEN s.is_warmup = 0 THEN s.weight_kg END) AS max_weight,"
        " SUM(CASE WHEN s.is_warmup = 0 THEN s.reps END) AS total_reps,"
        " SUM(CASE WHEN s.is_warmup = 0 THEN s.weight_kg * s.reps END) AS volume,"
        " MIN(w.workout_date) AS first_date, MAX(w.workout_date) AS last_date"
        + exercise_sql + " GROUP BY e.id ORDER BY sessions DESC, e.name_key LIMIT ?", (start, end, TOP_EXERCISES))]
    return {"workouts": row["workouts"], "days_with_workouts": row["days"],
            "duration_min_total": round_or_none(row["duration_min"]),
            "workouts_without_duration": row["without_duration"] or 0, "by_type": by_type,
            "exercises_total": total_exercises, "exercises": exercises,
            "exercises_truncated": total_exercises > len(exercises),
            "note": "Рабочие подходы — без разминочных. volume_kg — сумма вес × повторения по рабочим подходам."}


def _nutrition(conn, start, end):
    summary = nutrition.get_nutrition_summary(conn, start, end)
    days = summary["days"]
    known = [d for d in days if d["kcal"] is not None]

    def avg(key):
        values = [d[key] for d in known if d[key] is not None]
        return round(sum(values) / len(values), 1) if values else None

    return {"days_with_records": len(days), "days_with_kcal": len(known),
            "days_complete": sum(1 for d in days if d["totals_complete"]),
            "avg_per_day_with_kcal": {k: avg(k) for k in nutrition.NUTRIENTS},
            "totals": summary["totals"],
            "items_without_kcal": sum(d["items_without_kcal"] for d in days),
            "items_estimated": sum(d["items_estimated"] or 0 for d in days),
            "note": "Средние — по дням, где известны калории; дни без записей не считаются нулевыми."}


def _body(conn, start, end):
    weights = conn.execute(
        "SELECT measured_on, weight_kg FROM measurement_sessions WHERE measured_on BETWEEN ? AND ?"
        " AND weight_kg IS NOT NULL ORDER BY measured_on, measured_time, id", (start, end)).fetchall()
    weight = None
    if weights:
        values = [r["weight_kg"] for r in weights]
        weight = {"measurements": len(weights), "first": {"date": weights[0]["measured_on"], "kg": values[0]},
                  "last": {"date": weights[-1]["measured_on"], "kg": values[-1]}, "min_kg": min(values),
                  "max_kg": max(values), "change_kg": round(values[-1] - values[0], 2)}
    measurements = {}
    for r in conn.execute(
            "SELECT b.type_code, s.measured_on, b.value FROM body_measurements b"
            " JOIN measurement_sessions s ON s.id = b.session_id WHERE s.measured_on BETWEEN ? AND ?"
            " ORDER BY s.measured_on, s.measured_time, s.id", (start, end)):
        item = measurements.setdefault(r["type_code"], {"count": 0, "first": None, "last": None})
        item["count"] += 1
        point = {"date": r["measured_on"], "value": r["value"]}
        item["first"] = item["first"] or point
        item["last"] = point
    for item in measurements.values():
        item["change"] = round(item["last"]["value"] - item["first"]["value"], 2)
    return {"weight": weight, "measurements": measurements}


def _alcohol(conn, start, end):
    summary = alcohol.get_alcohol_summary(conn, start, end)
    return {k: summary[k] for k in ("entries_count", "days_with_alcohol", "ethanol_g_total", "entries_without_ethanol",
                                    "kcal_total", "entries_without_kcal")}


def _notes(conn, start, end):
    return {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM notes WHERE note_date BETWEEN ? AND ? GROUP BY category ORDER BY n DESC",
        (start, end))}


def _health(conn, start, end):
    from .health_status import _period_numbers
    try:
        return _period_numbers(conn, start, end)[0]
    except sqlite3.Error as exc:
        return {"error": f"данные Health Connect недоступны: {exc}"}


def period_stats(conn, date_from, date_to):
    start, end = v.parse_date_range(date_from, date_to)
    return {
        "date_from": start, "date_to": end,
        "workouts": _workouts(conn, start, end),
        "nutrition": _nutrition(conn, start, end),
        "body": _body(conn, start, end),
        "alcohol": _alcohol(conn, start, end),
        "notes_by_category": _notes(conn, start, end),
        "health": _health(conn, start, end),
        "note": ("Только агрегаты по фактическим записям базы; пустые разделы означают, что записей нет. "
                 "Подробности: get_workouts (detail='summary' или 'full'), get_nutrition_summary, get_measurements, "
                 "get_health_summary, get_alcohol_summary."),
    }
