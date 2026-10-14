"""Дневные сводки Health Connect для панели. Только SELECT поверх read-only соединения.

Правила подсчёта:
- учитываются только записи без deleted_at (удалённые в Health Connect остаются в базе с отметкой);
- шаги: сумма count интервалов за местную дату (local_date, по началу интервала). Если интервалы одного источника
  перекрываются, повторно покрытая часть не считается (шаги внутри интервала считаются равномерными). Если за день
  есть шаги от нескольких источников, берётся наибольшая сумма источника, а не их сложение;
- пульс: только отдельные измерения из heart_rate_samples; heart_rate_records — контейнеры измерений, их не
  суммируют. Повтор измерения (тот же источник и то же время) учитывается один раз. День измерения — местная дата
  его времени по смещению записи, а без смещения — дата записи;
- сон: сессия относится к дате пробуждения (local_date). «В постели» — объединение интервалов сессий за день,
  «во сне» — объединение стадий light/deep/rem/sleeping, обрезанных границами своей сессии.
"""

from datetime import date, datetime, timedelta

ASLEEP_STAGES = ("light", "deep", "rem", "sleeping")
AWAKE_STAGES = ("awake", "awake_in_bed", "out_of_bed")
STAGE_COLUMNS = ("deep", "light", "rem", "awake")   # разбивка в таблице; awake включает все «бодрствующие» стадии


def _ts(value):
    """'YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ' -> datetime (UTC, наивный). Наносекунды отбрасываются до микросекунд."""
    return datetime.fromisoformat(value[:26])


def _minutes(start, end):
    return (end - start).total_seconds() / 60


def _union_minutes(intervals):
    """Длина объединения интервалов [(start, end)] в минутах: перекрытия не считаются дважды."""
    total, cur_start, cur_end = 0.0, None, None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += _minutes(cur_start, cur_end)
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        total += _minutes(cur_start, cur_end)
    return total


def _day_before(iso_day):
    return (date.fromisoformat(iso_day) - timedelta(days=1)).isoformat()


# ---------- Общие сведения ----------

def counts(conn):
    """Число действующих записей Health Connect по видам."""
    steps = conn.execute("SELECT COUNT(*) FROM step_intervals WHERE deleted_at IS NULL").fetchone()[0]
    hr = conn.execute(
        "SELECT COUNT(*) FROM heart_rate_samples x JOIN heart_rate_records r ON r.id = x.record_id"
        " WHERE r.deleted_at IS NULL").fetchone()[0]
    sleep = conn.execute("SELECT COUNT(*) FROM sleep_sessions WHERE deleted_at IS NULL").fetchone()[0]
    return {"steps": steps, "heart_rate": hr, "sleep": sleep}


def earliest_date(conn):
    """Самая ранняя местная дата среди действующих записей Health Connect или None."""
    dates = [conn.execute(f"SELECT MIN(local_date) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]
             for table in ("step_intervals", "heart_rate_records", "sleep_sessions")]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


# ---------- Шаги ----------

def daily_steps(conn, date_from, date_to):
    """[{date, steps, intervals, sources, overlap}] по возрастанию даты; только дни с шагами."""
    # День до начала периода нужен, чтобы перекрытие с интервалом накануне не посчитать дважды.
    rows = conn.execute(
        "SELECT source_id, local_date, start_utc, end_utc, count FROM step_intervals"
        " WHERE deleted_at IS NULL AND local_date BETWEEN ? AND ?"
        " ORDER BY source_id, start_utc, end_utc, id",
        (_day_before(date_from), date_to)).fetchall()
    per_source = {}          # (date, source) -> [steps, intervals, overlap]
    covered_until = {}       # source -> конец уже учтённого времени
    for r in rows:
        start, end = _ts(r["start_utc"]), _ts(r["end_utc"])
        done = covered_until.get(r["source_id"])
        overlap = done is not None and start < done
        if not overlap:
            steps = r["count"]
        elif end <= done:
            steps = 0
        else:
            steps = r["count"] * _minutes(done, end) / _minutes(start, end)
        if done is None or end > done:
            covered_until[r["source_id"]] = end
        if r["local_date"] < date_from:
            continue
        cell = per_source.setdefault((r["local_date"], r["source_id"]), [0.0, 0, False])
        cell[0] += steps
        cell[1] += 1
        cell[2] = cell[2] or overlap

    days = {}
    for (day, _source), (steps, intervals, overlap) in per_source.items():
        d = days.setdefault(day, {"date": day, "steps": 0, "intervals": 0, "sources": 0, "overlap": False})
        d["steps"] = max(d["steps"], round(steps))
        d["intervals"] += intervals
        d["sources"] += 1
        d["overlap"] = d["overlap"] or overlap
    return [days[k] for k in sorted(days)]


# ---------- Пульс ----------

def daily_heart_rate(conn, date_from, date_to):
    """[{date, min_bpm, avg_bpm, max_bpm, samples}] по возрастанию даты; только дни с измерениями."""
    rows = conn.execute(
        "SELECT day, MIN(bpm) AS min_bpm, AVG(bpm) AS avg_bpm, MAX(bpm) AS max_bpm, COUNT(*) AS samples FROM ("
        "  SELECT day, source_id, time_utc, AVG(bpm) AS bpm FROM ("
        "    SELECT CASE WHEN r.start_offset_min IS NULL THEN r.local_date"
        "                ELSE date(substr(x.time_utc, 1, 19), r.start_offset_min || ' minutes') END AS day,"
        "           r.source_id, x.time_utc, x.bpm"
        "    FROM heart_rate_samples x JOIN heart_rate_records r ON r.id = x.record_id"
        "    WHERE r.deleted_at IS NULL AND r.local_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')"
        "  ) GROUP BY day, source_id, time_utc"
        ") WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day",
        (date_from, date_to, date_from, date_to)).fetchall()
    return [{"date": r["day"], "min_bpm": _bpm(r["min_bpm"]), "avg_bpm": round(r["avg_bpm"], 1),
             "max_bpm": _bpm(r["max_bpm"]), "samples": r["samples"]} for r in rows]


def _bpm(value):
    """Минимум и максимум целые, кроме случая, когда повтор измерения с разными значениями дал среднее."""
    return int(value) if value == int(value) else round(value, 1)


def heart_rate_period(days):
    """Сводка периода по дневным данным: среднее по всем измерениям, общий минимум и максимум."""
    samples = sum(d["samples"] for d in days)
    if not samples:
        return None
    return {"avg_bpm": round(sum(d["avg_bpm"] * d["samples"] for d in days) / samples, 1),
            "min_bpm": min(d["min_bpm"] for d in days), "max_bpm": max(d["max_bpm"] for d in days),
            "samples": samples, "days": len(days)}


# ---------- Сон ----------

def daily_sleep(conn, date_from, date_to):
    """[{date, sessions, in_bed_min, asleep_min, deep_min, light_min, rem_min, awake_min, staged_sessions}]
    по возрастанию даты. Показатели по стадиям — None, если ни у одной сессии дня нет стадий."""
    sessions = conn.execute(
        "SELECT id, local_date, start_utc, end_utc FROM sleep_sessions"
        " WHERE deleted_at IS NULL AND local_date BETWEEN ? AND ? ORDER BY local_date, start_utc, id",
        (date_from, date_to)).fetchall()
    if not sessions:
        return []
    stages = {}
    for st in conn.execute(
            "SELECT st.session_id, st.stage, st.start_utc, st.end_utc FROM sleep_stages st"
            " JOIN sleep_sessions s ON s.id = st.session_id"
            " WHERE s.deleted_at IS NULL AND s.local_date BETWEEN ? AND ?",
            (date_from, date_to)):
        stages.setdefault(st["session_id"], []).append(st)

    by_day = {}
    for s in sessions:
        start, end = _ts(s["start_utc"]), _ts(s["end_utc"])
        d = by_day.setdefault(s["local_date"], {"sessions": [], "stages": {}, "staged": 0})
        d["sessions"].append((start, end))
        own = stages.get(s["id"], [])
        if own:
            d["staged"] += 1
        for st in own:
            # Стадия за пределами своей сессии обрезается её границами.
            a, b = max(_ts(st["start_utc"]), start), min(_ts(st["end_utc"]), end)
            if b > a:
                d["stages"].setdefault(st["stage"], []).append((a, b))

    result = []
    for day in sorted(by_day):
        d = by_day[day]
        item = {"date": day, "sessions": len(d["sessions"]), "staged_sessions": d["staged"],
                "in_bed_min": round(_union_minutes(d["sessions"]), 1)}
        if d["staged"]:
            st = d["stages"]
            item["asleep_min"] = round(_union_minutes([iv for k in ASLEEP_STAGES for iv in st.get(k, [])]), 1)
            item["awake_min"] = round(_union_minutes([iv for k in AWAKE_STAGES for iv in st.get(k, [])]), 1)
            for k in ("deep", "light", "rem"):
                item[f"{k}_min"] = round(_union_minutes(st.get(k, [])), 1)
        else:
            item.update(asleep_min=None, awake_min=None, deep_min=None, light_min=None, rem_min=None)
        result.append(item)
    return result
