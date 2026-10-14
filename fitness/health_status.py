"""Состояние синхронизации Health Connect и сводки шагов, пульса и сна. Только чтение.

База открывается через web.readonly (mode=ro, query_only, authorizer только на чтение): эти функции не могут
ничего изменить ни в базе, ни в папках. Используются MCP-инструментами, панелью и health_inbox.py --status.

Даты:
- local_date записи — местная дата по смещению часового пояса самой записи (шаги и пульс по началу, сон — дата
  пробуждения), её считает импортёр; дата экспорта и время импорта на неё не влияют;
- «сегодня» для режима days — местная дата компьютера, на котором работает сервер;
- время импорта и экспорта — UTC (суффикс Z).
"""

from datetime import date, datetime, timedelta, timezone

from . import db, health_inbox
from .errors import TrackerError, ValidationError
from .web import health as stats
from .web.readonly import DatabaseUnavailable, readonly_connection

MAX_DAYS = 400
MAX_ACTIVITY_DAYS = 93     # строка на каждый день: длиннее — слишком большой ответ для ChatGPT
RECORD_TABLES = {"steps": "step_intervals", "heart_rate": "heart_rate_records", "sleep": "sleep_sessions"}
STALE_AFTER_HOURS = 36


def _first_line(text, limit=300):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _utc_now():
    return datetime.now(timezone.utc)


# ---------- Состояние синхронизации ----------

def _journal(conn):
    last_ok = conn.execute(
        "SELECT id, file_name, phone_id, sequence, exported_at, imported_at FROM import_batches"
        " WHERE status = 'ok' ORDER BY imported_at DESC, id DESC LIMIT 1").fetchone()
    totals = conn.execute(
        "SELECT SUM(status = 'ok'), SUM(status = 'failed') FROM import_batches").fetchone()
    failures = conn.execute(
        "SELECT id, file_name, phone_id, sequence, imported_at, error FROM import_batches"
        " WHERE status = 'failed' ORDER BY id DESC LIMIT 5").fetchall()
    phones = conn.execute(
        "SELECT phone_id, MAX(sequence) AS last_sequence, MAX(exported_at) AS last_exported_at,"
        " MAX(imported_at) AS last_imported_at, COUNT(*) AS files FROM import_batches WHERE status = 'ok'"
        " GROUP BY phone_id ORDER BY phone_id").fetchall()
    last_ok_id = last_ok["id"] if last_ok else 0
    return {
        "last_successful_import": None if last_ok is None else {
            "import_id": last_ok["id"], "imported_at": last_ok["imported_at"], "exported_at": last_ok["exported_at"],
            "phone_id": last_ok["phone_id"], "sequence": last_ok["sequence"], "file": last_ok["file_name"]},
        "imported_files": totals[0] or 0,
        "failed_attempts": totals[1] or 0,
        "recent_failures": [{"import_id": r["id"], "file": r["file_name"], "sequence": r["sequence"],
                             "attempted_at": r["imported_at"], "error": _first_line(r["error"]),
                             "after_last_success": r["id"] > last_ok_id} for r in failures],
        "phones": [dict(r) for r in phones],
    }


def _freshness(conn, now):
    result = {}
    for kind, table in RECORD_TABLES.items():
        row = conn.execute(f"SELECT COUNT(*), MAX(end_utc), MAX(local_date), MIN(local_date) FROM {table}"
                           " WHERE deleted_at IS NULL").fetchone()
        last = row[1]
        hours = None
        if last:
            hours = round((now - datetime.fromisoformat(last[:19]).replace(tzinfo=timezone.utc))
                          .total_seconds() / 3600, 1)
        result[kind] = {"records": row[0], "last_record_end_utc": last[:19] + "Z" if last else None,
                        "last_local_date": row[2], "first_local_date": row[3], "hours_since_last_record": hours}
    return result


def sync_status(db_path=None, now=None):
    """Сводка: журнал импортов, свежесть данных по типам и состояние входящей папки."""
    now = now or _utc_now()
    status = {"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "database": {"ok": False, "path": str(db.resolve_db_path(db_path)), "error": None}}
    try:
        with readonly_connection(db_path) as conn:
            status.update(_journal(conn))
            status["data"] = _freshness(conn, now)
        status["database"]["ok"] = True
    except DatabaseUnavailable as exc:
        status["database"]["error"] = str(exc)
    status["inbox"] = health_inbox.inbox_status(db_path)
    status["attention"] = _attention(status)
    return status


def _attention(status):
    """Короткий список того, что требует внимания пользователя (пустой — всё в порядке)."""
    notes = []
    if not status["database"]["ok"]:
        notes.append(f"База недоступна: {status['database']['error']}")
        return notes
    inbox = status["inbox"]
    sync = inbox.get("syncthing") or {}
    if sync.get("old_setup"):
        notes.append("Syncthing ещё принимает файлы прямо в data\\health_inbox: перенастройте папку на "
                     "data\\health_syncthing (docs/HEALTH_SYNCTHING.md), иначе Syncthing будет просить "
                     "«Отменить локальные изменения»")
    if sync.get("not_handed_off"):
        notes.append(f"Файлов в папке Syncthing, ещё не переданных на импорт: {sync['not_handed_off']}")
    handoff = (inbox.get("last_run") or {}).get("handoff") or {}
    if handoff.get("errors"):
        notes.append(f"Ошибки передачи из папки Syncthing в последнем запуске: {len(handoff['errors'])}")
    if inbox["failed_files"]:
        notes.append(f"Файлов с ошибкой в health_failed: {inbox['failed_files']}")
    held = [w for w in inbox["waiting"] if w["outcome"] == "held"]
    if held:
        notes.append(f"Файлов ожидает во входящей папке (пропуск номеров): {len(held)}")
    new = [w for w in inbox["waiting"] if w["outcome"] == "new"]
    if new:
        notes.append(f"Необработанных файлов во входящей папке: {len(new)}")
    unresolved = [f for f in status.get("recent_failures", []) if f["after_last_success"]]
    if unresolved:
        notes.append(f"После последнего успешного импорта были неудачные попытки: {len(unresolved)}")
    if status.get("last_successful_import") is None:
        notes.append("Ни одного успешного импорта ещё не было")
    else:
        hours = [d["hours_since_last_record"] for d in status["data"].values() if d["hours_since_last_record"] is not None]
        if hours and min(hours) > STALE_AFTER_HOURS:
            notes.append(f"Самые свежие данные старше {STALE_AFTER_HOURS} ч: пора сделать экспорт на телефоне")
    last_run = inbox.get("last_run")
    if last_run and last_run.get("error"):
        notes.append(f"Последний автоматический импорт остановлен: {_first_line(last_run['error'].get('message'))}")
    return notes


def describe(status):
    """Текст для командной строки."""
    lines = [f"Проверено: {status['checked_at']}"]
    if not status["database"]["ok"]:
        lines.append(f"БАЗА НЕДОСТУПНА: {status['database']['error']}")
    else:
        last = status["last_successful_import"]
        lines.append("Последний успешный импорт: " + (
            f"{last['imported_at']} (файл №{last['sequence']}, экспорт {last['exported_at']})" if last else "ещё не было"))
        lines.append(f"Импортировано файлов: {status['imported_files']}, неудачных попыток: {status['failed_attempts']}")
        names = {"steps": "Шаги", "heart_rate": "Пульс", "sleep": "Сон"}
        for kind, d in status["data"].items():
            lines.append(f"  {names[kind]}: записей {d['records']}, последняя дата {d['last_local_date'] or '—'}"
                         + (f" (конец записи {d['last_record_end_utc']})" if d["last_record_end_utc"] else ""))
        for f in status["recent_failures"][:3]:
            lines.append(f"  Неудача {f['attempted_at']} ({f['file']}): {f['error']}")
    inbox = status["inbox"]
    sync = inbox.get("syncthing") or {}
    lines.append(f"Папка Syncthing: {inbox['folders']['syncthing']}"
                 + (f" (файлов {sync['files']}, ещё не передано на импорт {sync['not_handed_off']})" if sync.get("exists")
                    else " (не настроена: Syncthing создаст её сам)"))
    lines.append(f"Входящая папка: {inbox['folders']['inbox']}"
                 + ("" if inbox["inbox_exists"] else " (ещё не создана: появится при первом запуске импорта)"))
    lines.append(f"  ждут обработки: {inbox['waiting_files']}, с ошибкой: {inbox['failed_files']}, "
                 f"обработано: {inbox['processed_files']}")
    for w in inbox["waiting"]:
        lines.append(f"  {w['file']}: {w['reason'] or 'новый, ещё не обрабатывался'}")
    for f in inbox["failed"][:5]:
        lines.append(f"  ОШИБКА {f['file']}: {f['error']}")
    check = inbox.get("last_check")
    lines.append("Последняя проверка папки: " + (check["checked_at"] if check else "ещё не было"))
    if status["attention"]:
        lines.append("Требует внимания:")
        lines.extend(f"  - {n}" for n in status["attention"])
    else:
        lines.append("Всё в порядке.")
    return lines


# ---------- Сводка шагов, пульса и сна ----------

def resolve_period(date_from=None, date_to=None, days=None, today=None, max_days=MAX_DAYS):
    """(date_from, date_to) по явным датам или по числу дней, заканчивающихся сегодня (местная дата компьютера)."""
    if days is not None:
        if date_from is not None or date_to is not None:
            raise ValidationError("days", "передайте либо days, либо date_from и date_to")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
            raise ValidationError("days", f"целое 1…{MAX_DAYS}")
        end = today or date.today()
        return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()
    if date_from is None or date_to is None:
        raise ValidationError("date_from", "нужен период: date_from и date_to (YYYY-MM-DD) или days")
    parsed = []
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        try:
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError
            parsed.append(date.fromisoformat(value))
        except ValueError:
            raise ValidationError(name, f"нужна дата YYYY-MM-DD, получено {value!r}") from None
    if parsed[1] < parsed[0]:
        raise ValidationError("date_to", "конец периода раньше начала")
    if (parsed[1] - parsed[0]).days + 1 > max_days:
        raise ValidationError("date_to", f"период не длиннее {max_days} дней")
    return date_from, date_to


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _period_numbers(conn, date_from, date_to):
    steps = stats.daily_steps(conn, date_from, date_to)
    hr_days = stats.daily_heart_rate(conn, date_from, date_to)
    sleep = stats.daily_sleep(conn, date_from, date_to)
    hr = stats.heart_rate_period(hr_days)
    staged = [d for d in sleep if d["asleep_min"] is not None]
    return {
        "steps": {"days_with_data": len(steps), "total": sum(d["steps"] for d in steps),
                  "avg_per_day": round(sum(d["steps"] for d in steps) / len(steps)) if steps else None,
                  "max_day": max(steps, key=lambda d: d["steps"])["date"] if steps else None,
                  "max_steps": max(d["steps"] for d in steps) if steps else None},
        "heart_rate": hr or {"samples": 0, "days": 0, "avg_bpm": None, "min_bpm": None, "max_bpm": None},
        "sleep": {"nights": len(sleep), "avg_in_bed_min": _avg([d["in_bed_min"] for d in sleep]),
                  "avg_asleep_min": _avg([d["asleep_min"] for d in staged]),
                  "avg_deep_min": _avg([d["deep_min"] for d in staged]),
                  "avg_light_min": _avg([d["light_min"] for d in staged]),
                  "avg_rem_min": _avg([d["rem_min"] for d in staged]),
                  "avg_awake_min": _avg([d["awake_min"] for d in staged]),
                  "nights_with_stages": len(staged)},
    }, steps, hr_days, sleep


def _delta(cur, prev):
    if cur is None or prev is None:
        return None
    return round(cur - prev, 1)


def health_summary(db_path=None, date_from=None, date_to=None, days=None, include_days=True, compare=True,
                   today=None):
    """Шаги, пульс и сон за период по фактической базе (+ сравнение с предыдущим периодом той же длины)."""
    date_from, date_to = resolve_period(date_from, date_to, days, today)
    length = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    try:
        with readonly_connection(db_path) as conn:
            current, steps, hr_days, sleep = _period_numbers(conn, date_from, date_to)
            result = {"period": {"date_from": date_from, "date_to": date_to, "days": length,
                                 "today_local": (today or date.today()).isoformat()},
                      "summary": current,
                      "data_available": {k: {"first_local_date": v["first_local_date"],
                                             "last_local_date": v["last_local_date"]}
                                         for k, v in _freshness(conn, _utc_now()).items()}}
            if include_days:
                result["days"] = {"steps": [{"date": d["date"], "steps": d["steps"], "sources": d["sources"]}
                                            for d in steps],
                                  "heart_rate": hr_days, "sleep": sleep}
            if compare:
                prev_to = date.fromisoformat(date_from) - timedelta(days=1)
                prev_from = prev_to - timedelta(days=length - 1)
                previous, *_ = _period_numbers(conn, prev_from.isoformat(), prev_to.isoformat())
                result["previous_period"] = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat(),
                                             "summary": previous}
                result["change"] = {
                    "steps_avg_per_day": _delta(current["steps"]["avg_per_day"], previous["steps"]["avg_per_day"]),
                    "heart_rate_avg_bpm": _delta(current["heart_rate"]["avg_bpm"], previous["heart_rate"]["avg_bpm"]),
                    "sleep_avg_in_bed_min": _delta(current["sleep"]["avg_in_bed_min"], previous["sleep"]["avg_in_bed_min"]),
                    "sleep_avg_asleep_min": _delta(current["sleep"]["avg_asleep_min"], previous["sleep"]["avg_asleep_min"]),
                }
    except DatabaseUnavailable as exc:
        raise TrackerError(str(exc)) from None
    notes = ["Шаги за день — сумма интервалов по местной дате; при нескольких источниках берётся наибольший, а не сумма.",
             "Пульс — по отдельным измерениям часов; среднее зависит от того, когда часы мерили.",
             "Сон относится к дате пробуждения; asleep (во сне) считается только по стадиям."]
    last_dates = [v["last_local_date"] for v in result["data_available"].values() if v["last_local_date"]]
    if last_dates and max(last_dates) < date_to:
        notes.append(f"Данные в базе есть только до {max(last_dates)}: дни после неё пустые, потому что ещё не было "
                     "импорта, а не потому что не было активности.")
    result["notes"] = notes
    return result


# ---------- Активность по дням ----------

def daily_activity(db_path=None, date_from=None, date_to=None, today=None):
    """Каждый календарный день периода отдельной строкой: шаги, сон, пульс. Нет данных — null, а не 0."""
    if date_from is None or date_to is None:
        raise ValidationError("date_from" if date_from is None else "date_to", "нужен период: date_from и date_to (YYYY-MM-DD)")
    date_from, date_to = resolve_period(date_from, date_to, None, today, max_days=MAX_ACTIVITY_DAYS)
    try:
        with readonly_connection(db_path) as conn:
            steps = {d["date"]: d for d in stats.daily_steps(conn, date_from, date_to)}
            hr = {d["date"]: d for d in stats.daily_heart_rate(conn, date_from, date_to)}
            sleep = {d["date"]: d for d in stats.daily_sleep(conn, date_from, date_to)}
            available = {k: {"first_local_date": v["first_local_date"], "last_local_date": v["last_local_date"]}
                         for k, v in _freshness(conn, _utc_now()).items()}
    except DatabaseUnavailable as exc:
        raise TrackerError(str(exc)) from None

    def status(kind, day, present):
        if present:
            return "ok"
        first, last = available[kind]["first_local_date"], available[kind]["last_local_date"]
        if last is None or day > last:
            return "not_imported_yet"
        return "before_first_record" if day < first else "no_records"

    days = []
    current, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    while current <= end:
        day = current.isoformat()
        s, h, n = steps.get(day), hr.get(day), sleep.get(day)
        days.append({
            "date": day,
            "steps": s["steps"] if s else None,
            "steps_status": status("steps", day, s),
            "sleep": None if n is None else {k: n[k] for k in ("in_bed_min", "asleep_min", "deep_min", "light_min",
                                                                 "rem_min", "awake_min", "sessions")},
            "sleep_status": status("sleep", day, n),
            "heart_rate": None if h is None else {k: h[k] for k in ("min_bpm", "avg_bpm", "max_bpm", "samples")},
            "heart_rate_status": status("heart_rate", day, h),
        })
        current += timedelta(days=1)

    with_steps = [d["steps"] for d in days if d["steps"] is not None]
    return {
        "period": {"date_from": date_from, "date_to": date_to, "days": len(days)},
        "totals": {"days_with_steps": len(with_steps), "days_without_steps": len(days) - len(with_steps),
                   "steps_total": sum(with_steps) if with_steps else None,
                   "steps_avg_per_day_with_data": round(sum(with_steps) / len(with_steps)) if with_steps else None},
        "days": days,
        "data_available": available,
        "notes": [
            "steps — шаги за местную календарную дату (по началу интервала, часовой пояс записи). Перекрывающиеся "
            "интервалы одного источника не суммируются дважды; при нескольких источниках берётся наибольший.",
            "null означает отсутствие данных, а не ноль: не подставляй 0 в расчёт активности и калорий. "
            "*_status: ok — данные есть; not_imported_yet — день позже последнего импорта; no_records — день внутри "
            "импортированного диапазона без записей (например, телефон или часы не носили); "
            "before_first_record — раньше первых данных в базе.",
            "sleep относится к дате пробуждения; asleep_min и стадии null, если у сна нет стадий. "
            "heart_rate — по отдельным измерениям часов.",
        ],
    }
