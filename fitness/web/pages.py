"""HTML-страницы панели. Только чтение: используются готовые функции get_* поверх read-only соединения."""

from datetime import date, timedelta
from html import escape
from urllib.parse import urlencode

from .. import alcohol, measurements, nutrition, workouts
from ..validation import MAX_RANGE_DAYS
from . import charts, health

PERIODS = {"30": "30 дней", "90": "90 дней", "365": "Год", "all": "Всё время"}
DEFAULT_PERIOD = "90"

NAV = [
    ("/", "Обзор"),
    ("/measurements", "Вес и замеры"),
    ("/workouts", "Тренировки"),
    ("/nutrition", "Питание"),
    ("/alcohol", "Алкоголь"),
    ("/health", "Здоровье"),
    ("/photos", "Фото"),
    ("/sync", "Синхронизация"),
]

WORKOUT_TYPES_RU = {"strength": "силовая", "cardio": "кардио", "mobility": "мобильность", "sport": "спорт",
                    "other": "другое"}
MEAL_TYPES_RU = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин", "snack": "перекус", "other": "другое"}
UNITS_RU = {"g": "г", "ml": "мл", "pcs": "шт", "serving": "порц."}
VIEWS_RU = {"front": "спереди", "side": "сбоку", "back": "сзади", "other": "другое"}

_DATE_COLUMNS = [("measurement_sessions", "measured_on"), ("workouts", "workout_date"), ("meals", "eaten_on"),
                 ("alcohol_entries", "consumed_on"), ("notes", "note_date"), ("photos", "taken_on")]

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;--accent2:#93b4f5;
--warn:#b54708;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b2027;--text:#e6e9ee;--muted:#98a2b3;--line:#2c333d;
--accent:#6ea0ff;--accent2:#35508a;--warn:#f79009}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px}
header h1{margin:0 0 8px;font-size:18px}nav a{margin-right:14px;color:var(--muted);text-decoration:none;
display:inline-block;padding:2px 0}nav a.active{color:var(--accent);font-weight:600;border-bottom:2px solid var(--accent)}
main{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px;
overflow-x:auto}.card h2{margin:0 0 10px;font-size:16px}
.periods a,.chips a{display:inline-block;margin:0 6px 6px 0;padding:3px 10px;border:1px solid var(--line);
border-radius:999px;color:var(--text);text-decoration:none;font-size:13px}
.periods a.active,.chips a.active{background:var(--accent);border-color:var(--accent);color:#fff}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;white-space:nowrap}
.muted{color:var(--muted)}.warn{color:var(--warn)}.empty{color:var(--muted);padding:12px 0}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.stat{border:1px solid var(--line);border-radius:8px;padding:10px}.stat b{display:block;font-size:22px}
.chart{width:100%;height:auto;max-height:260px}.chart .grid{stroke:var(--line)}
.chart .axis{fill:var(--muted);font-size:11px}.chart .line{fill:none;stroke:var(--accent);stroke-width:2}
.chart .dot{fill:var(--accent)}.chart .bar{fill:var(--accent)}
.chart .range{stroke:var(--accent2);stroke-width:4;stroke-linecap:round}
.workout{border-top:1px solid var(--line);padding:10px 0}.workout:first-of-type{border-top:0}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.photos figure{margin:0}.photos img{width:100%;border-radius:8px;border:1px solid var(--line)}
footer{max-width:1000px;margin:0 auto;padding:0 16px 24px;color:var(--muted);font-size:12px}
"""


# ---------- Общие помощники ----------

def fmt(value, digits=1, unit=""):
    if value is None:
        return '<span class="muted">—</span>'
    if isinstance(value, float):
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return escape(text + (f" {unit}" if unit else ""))


def text(value):
    return escape(value) if value else ""


def fmt_int(value):
    """Целое с неразрывными пробелами между разрядами: 12 345."""
    if value is None:
        return '<span class="muted">—</span>'
    return f"{round(value):,}".replace(",", " ")


def fmt_duration(minutes):
    """Минуты -> «7 ч 05 мин»."""
    if minutes is None:
        return '<span class="muted">—</span>'
    hours, mins = divmod(round(minutes), 60)
    return f"{hours} ч {mins:02d} мин" if hours else f"{mins} мин"


def period_bounds(conn, period, today=None):
    """Период -> (date_from, date_to). 'all' — с самой ранней записи, не длиннее допустимого диапазона."""
    today = today or date.today()
    if period == "all":
        earliest = None
        for table, column in _DATE_COLUMNS:
            row = conn.execute(f"SELECT MIN({column}) FROM {table}").fetchone()
            if row[0] and (earliest is None or row[0] < earliest):
                earliest = row[0]
        health_earliest = health.earliest_date(conn)
        if health_earliest and (earliest is None or health_earliest < earliest):
            earliest = health_earliest
        start = date.fromisoformat(earliest) if earliest else today
        start = max(start, today - timedelta(days=MAX_RANGE_DAYS))
    else:
        start = today - timedelta(days=int(period) - 1)
    return start.isoformat(), today.isoformat()


def link(path, **params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return escape(path + ("?" + urlencode(params) if params else ""))


def period_selector(path, period, **params):
    items = []
    for key, label in PERIODS.items():
        cls = ' class="active"' if key == period else ""
        items.append(f'<a{cls} href="{link(path, period=key, **params)}">{label}</a>')
    return f'<div class="periods">{"".join(items)}</div>'


def empty(message="Пока нет записей за этот период."):
    return f'<p class="empty">{escape(message)}</p>'


def layout(title, active, body):
    links = []
    for href, label in NAV:
        cls = ' class="active"' if href == active else ""
        links.append(f'<a href="{href}"{cls}>{escape(label)}</a>')
    nav = "".join(links)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} — FITNESS Tracker</title><style>{CSS}</style></head><body>"
        f"<header><h1>FITNESS Tracker</h1><nav>{nav}</nav></header><main>{body}</main>"
        "<footer>Аналитика только для чтения: данные добавляются через ChatGPT, фото — в разделе «Фото». "
        "Приблизительные значения отмечены ≈.</footer></body></html>"
    )


def unavailable_page(message):
    body = f'<div class="card"><h2>База недоступна</h2><p>{escape(message)}</p></div>'
    return layout("База недоступна", "/", body)


def not_found_page():
    return layout("Не найдено", "", '<div class="card"><h2>Страница не найдена</h2></div>')


# ---------- Страницы ----------

def overview_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    counts = {
        "Замеры": conn.execute("SELECT COUNT(*) FROM measurement_sessions").fetchone()[0],
        "Тренировки": conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0],
        "Приёмы пищи": conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0],
        "Алкоголь": conn.execute("SELECT COUNT(*) FROM alcohol_entries").fetchone()[0],
        "Заметки": conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        "Фото": conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
    }
    stats = "".join(f'<div class="stat"><span class="muted">{escape(k)}</span><b>{v}</b></div>'
                    for k, v in counts.items())
    parts = [f'<div class="card"><h2>Всего записей</h2><div class="stats">{stats}</div></div>']

    health_counts = health.counts(conn)
    has_health = sum(health_counts.values()) > 0
    if has_health:
        hstats = "".join(_stat(label, fmt_int(health_counts[key])) for key, label in (
            ("steps", "Интервалы шагов"), ("heart_rate", "Измерения пульса"), ("sleep", "Сессии сна")))
        parts.append(f'<div class="card"><h2>Health Connect</h2><div class="stats">{hstats}</div></div>')

    if sum(counts.values()) == 0:
        if has_health:
            parts.extend(_health_overview(conn, period, date_from, date_to, with_selector=True))
        else:
            parts.append('<div class="card"><h2>База пуста</h2><p>Записей пока нет. Расскажите ChatGPT о тренировке, '
                         "весе или приёме пищи — данные появятся здесь.</p></div>")
        return layout("Обзор", "/", "".join(parts))

    last_weight = conn.execute(
        "SELECT measured_on, weight_kg FROM measurement_sessions WHERE weight_kg IS NOT NULL"
        " ORDER BY measured_on DESC, measured_time DESC, id DESC LIMIT 1").fetchone()
    last_workout = conn.execute(
        "SELECT workout_date, title FROM workouts ORDER BY workout_date DESC, started_time DESC, id DESC LIMIT 1"
    ).fetchone()
    latest = []
    if last_weight:
        latest.append(f"<li>Последний вес: <b>{fmt(last_weight['weight_kg'], unit='кг')}</b> "
                      f"({escape(last_weight['measured_on'])})</li>")
    if last_workout:
        latest.append(f"<li>Последняя тренировка: <b>{escape(last_workout['title'])}</b> "
                      f"({escape(last_workout['workout_date'])})</li>")
    if latest:
        parts.append(f'<div class="card"><h2>Последние данные</h2><ul>{"".join(latest)}</ul></div>')

    weights = measurements.get_measurements(conn, date_from, date_to, "weight")["records"]
    chart = charts.line_chart([(r["measured_on"], r["weight_kg"]) for r in weights], "Вес, кг", "кг")
    parts.append(f'<div class="card"><h2>Вес за период</h2>{period_selector("/", period)}'
                 f'{chart or empty()}</div>')
    if has_health:
        parts.extend(_health_overview(conn, period, date_from, date_to, with_selector=False))
    return layout("Обзор", "/", "".join(parts))


def _stat(label, value):
    return f'<div class="stat"><span class="muted">{escape(label)}</span><b>{value}</b></div>'


def _steps_chart(days):
    return charts.bar_chart([(d["date"], d["steps"]) for d in days], "Шаги по дням", "шагов")


def _heart_chart(days):
    return charts.range_chart([(d["date"], d["min_bpm"], d["avg_bpm"], d["max_bpm"]) for d in days],
                              "Пульс по дням: среднее и диапазон, уд/мин", "уд/мин")


def _sleep_chart(days):
    return charts.bar_chart([(d["date"], round(d["in_bed_min"] / 60, 2)) for d in days], "Сон по дням, ч", "ч")


def _health_overview(conn, period, date_from, date_to, with_selector):
    """Карточки шагов, пульса и сна для обзора."""
    steps = health.daily_steps(conn, date_from, date_to)
    hr_days = health.daily_heart_rate(conn, date_from, date_to)
    sleep = health.daily_sleep(conn, date_from, date_to)
    selector = period_selector("/", period) if with_selector else ""

    if steps:
        total = sum(d["steps"] for d in steps)
        last = steps[-1]
        stats = (_stat("В среднем за день", fmt_int(total / len(steps))) + _stat("Всего за период", fmt_int(total))
                 + _stat(f"Последний день, {last['date']}", fmt_int(last["steps"])))
        steps_html = f'<div class="stats">{stats}</div>{_steps_chart(steps)}'
    else:
        steps_html = empty()

    period_hr = health.heart_rate_period(hr_days)
    if period_hr:
        last = hr_days[-1]
        stats = (_stat("Среднее за период", fmt(period_hr["avg_bpm"], unit="уд/мин"))
                 + _stat("Мин. – макс. за период", f"{period_hr['min_bpm']}–{period_hr['max_bpm']}")
                 + _stat(f"Последний день, {last['date']}",
                         f"{last['min_bpm']}–{last['max_bpm']}, ср. {fmt(last['avg_bpm'])}"))
        hr_html = f'<div class="stats">{stats}</div>{_heart_chart(hr_days)}'
    else:
        hr_html = empty()

    if sleep:
        avg = sum(d["in_bed_min"] for d in sleep) / len(sleep)
        last = sleep[-1]
        stats = (_stat("В среднем за сутки", fmt_duration(avg))
                 + _stat(f"Последние сутки, {last['date']}", fmt_duration(last["in_bed_min"]))
                 + _stat("Из них во сне (по стадиям)", fmt_duration(last["asleep_min"])))
        sleep_html = f'<div class="stats">{stats}</div>{_sleep_chart(sleep)}'
    else:
        sleep_html = empty()

    more = f'<p><a href="{link("/health", period=period)}">Подробнее в разделе «Здоровье»</a></p>'
    return [f'<div class="card"><h2>Шаги</h2>{selector}{steps_html}</div>',
            f'<div class="card"><h2>Пульс, уд/мин</h2>{hr_html}</div>',
            f'<div class="card"><h2>Сон</h2>{sleep_html}{more}</div>']


def measurements_page(conn, period, type_code=None):
    date_from, date_to = period_bounds(conn, period)
    types = measurements.measurement_types(conn)
    if type_code not in types:
        type_code = None
    data = measurements.get_measurements(conn, date_from, date_to)["records"]

    chips = ['<a{} href="{}">Вес</a>'.format(' class="active"' if type_code is None else "",
                                             link("/measurements", period=period))]
    for code, t in types.items():
        chips.append('<a{} href="{}">{}</a>'.format(' class="active"' if code == type_code else "",
                                                     link("/measurements", period=period, type=code),
                                                     escape(t["name_ru"])))
    if type_code is None:
        points = [(r["measured_on"], r["weight_kg"]) for r in data]
        title, unit = "Вес", "кг"
    else:
        t = types[type_code]
        unit = "см" if t["unit"] == "cm" else "%"
        points = [(r["measured_on"], m["value"]) for r in data for m in r["measurements"] if m["type"] == type_code]
        title = t["name_ru"]
    chart = charts.line_chart(points, f"{title}, {unit}", unit)

    parts = [f'<div class="card"><h2>{escape(title)}</h2>{period_selector("/measurements", period, type=type_code)}'
             f'<div class="chips">{"".join(chips)}</div>{chart or empty()}</div>']

    if data:
        rows = []
        for r in reversed(data):
            ms = ", ".join(f"{escape(m['name_ru'])}: {fmt(m['value'], unit='см' if m['unit'] == 'cm' else '%')}"
                           for m in r["measurements"])
            photos = len(r["photos"])
            rows.append(f"<tr><td>{escape(r['measured_on'])} {text(r['measured_time'])}</td>"
                        f"<td class=\"num\">{fmt(r['weight_kg'], unit='кг')}</td><td>{ms or ''}</td>"
                        f"<td>{text(r['notes'])}{' · фото: ' + str(photos) if photos else ''}</td></tr>")
        table = ("<table><tr><th>Дата</th><th class=\"num\">Вес</th><th>Замеры</th><th>Заметки</th></tr>"
                 + "".join(rows) + "</table>")
    else:
        table = empty()
    parts.append(f'<div class="card"><h2>История</h2>{table}</div>')
    return layout("Вес и замеры", "/measurements", "".join(parts))


def _set_line(s):
    bits = []
    if s["weight_kg"] is not None:
        bits.append(fmt(s["weight_kg"], unit="кг"))
    if s["reps"] is not None:
        bits.append(f"× {s['reps']}")
    if s["duration_sec"] is not None:
        bits.append(fmt(s["duration_sec"], unit="с"))
    if s["distance_m"] is not None:
        bits.append(fmt(s["distance_m"], unit="м"))
    if s["rir"] is not None:
        bits.append(f"RIR {fmt(s['rir'])}")
    line = " ".join(bits)
    if s["is_warmup"]:
        line += ' <span class="muted">(разминка)</span>'
    if s["notes"]:
        line += f' <span class="muted">— {escape(s["notes"])}</span>'
    return line


def workouts_page(conn, period, exercise=None):
    date_from, date_to = period_bounds(conn, period)
    names = [e["name"] for e in workouts.list_exercises(conn)]
    if exercise not in names:
        exercise = None
    data = workouts.get_workouts(conn, date_from, date_to, exercise=exercise)["records"]

    chips = ['<a{} href="{}">Все</a>'.format(' class="active"' if exercise is None else "",
                                             link("/workouts", period=period))]
    for name in names:
        chips.append('<a{} href="{}">{}</a>'.format(' class="active"' if name == exercise else "",
                                                     link("/workouts", period=period, exercise=name), escape(name)))
    parts = [f'<div class="card"><h2>Тренировки</h2>{period_selector("/workouts", period, exercise=exercise)}'
             f'<div class="chips">{"".join(chips)}</div></div>']

    if exercise:
        # Лучший рабочий вес в упражнении на каждую дату.
        best = {}
        for w in data:
            for ex in w["exercises"]:
                if ex["name"] != exercise:
                    continue
                weights = [s["weight_kg"] for s in ex["sets"] if s["weight_kg"] is not None and not s["is_warmup"]]
                if weights:
                    best[w["workout_date"]] = max(best.get(w["workout_date"], 0), max(weights))
        chart = charts.line_chart(sorted(best.items()), f"{exercise}: лучший рабочий вес, кг", "кг")
        parts.append(f'<div class="card"><h2>{escape(exercise)}: лучший рабочий вес</h2>'
                     f'{chart or empty("Нет подходов с весом за этот период.")}</div>')

    if not data:
        parts.append(f'<div class="card">{empty()}</div>')
    else:
        items = []
        for w in reversed(data):
            meta = [WORKOUT_TYPES_RU.get(w["workout_type"], w["workout_type"])]
            if w["started_time"]:
                meta.append(w["started_time"])
            if w["duration_min"] is not None:
                meta.append(f"{fmt(w['duration_min'])} мин")
            exs = []
            for ex in w["exercises"]:
                if exercise and ex["name"] != exercise:
                    continue
                sets = "".join(f"<li>{_set_line(s)}</li>" for s in ex["sets"])
                extra = ""
                if ex["discomfort"]:
                    extra += f'<div class="warn">Дискомфорт: {escape(ex["discomfort"])}</div>'
                if ex["notes"]:
                    extra += f'<div class="muted">{escape(ex["notes"])}</div>'
                exs.append(f"<li><b>{escape(ex['name'])}</b>{extra}<ol>{sets}</ol></li>")
            notes = f'<div class="muted">{escape(w["notes"])}</div>' if w["notes"] else ""
            items.append(f'<div class="workout"><b>{escape(w["workout_date"])}</b> — {escape(w["title"])} '
                         f'<span class="muted">({escape(", ".join(meta))})</span>{notes}<ul>{"".join(exs)}</ul></div>')
        parts.append(f'<div class="card"><h2>История</h2>{"".join(items)}</div>')
    return layout("Тренировки", "/workouts", "".join(parts))


def nutrition_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    summary = nutrition.get_nutrition_summary(conn, date_from, date_to, include_meals=True)
    days = summary["days"]
    chart = charts.bar_chart([(d["date"], d["kcal"]) for d in days], "Калории по дням", "ккал")
    parts = [f'<div class="card"><h2>Калории по дням</h2>{period_selector("/nutrition", period)}'
             f'{chart or empty()}</div>']
    if days:
        totals = summary["totals"]
        avg = summary["average_kcal_per_logged_day"]
        rows = []
        for d in reversed(days):
            flags = []
            if not d["totals_complete"]:
                flags.append(f'<span class="warn">без калорий: {d["items_without_kcal"]}</span>')
            if d["items_estimated"]:
                flags.append(f'≈ {d["items_estimated"]} из {d["items"]}')
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['meals']}</td>"
                        f"<td class=\"num\">{fmt(d['kcal'])}</td><td class=\"num\">{fmt(d['protein_g'])}</td>"
                        f"<td class=\"num\">{fmt(d['fat_g'])}</td><td class=\"num\">{fmt(d['carbs_g'])}</td>"
                        f"<td>{' · '.join(flags)}</td></tr>")
        parts.append(
            '<div class="card"><h2>Сводка по дням</h2>'
            f'<p>За период: {fmt(totals["kcal"], unit="ккал")}, Б {fmt(totals["protein_g"], unit="г")}, '
            f'Ж {fmt(totals["fat_g"], unit="г")}, У {fmt(totals["carbs_g"], unit="г")}. '
            f'В среднем {fmt(avg, unit="ккал")} в день с записями.</p>'
            '<p class="muted">Суммы только по известным значениям. ≈ — сколько продуктов с приблизительными '
            "данными.</p>"
            '<table><tr><th>Дата</th><th class="num">Приёмов</th><th class="num">Ккал</th><th class="num">Б</th>'
            '<th class="num">Ж</th><th class="num">У</th><th>Полнота</th></tr>' + "".join(rows) + "</table></div>"
        )
        meal_rows = []
        for m in reversed(summary["meals"]):
            for it in m["items"]:
                qty = f"{fmt(it['quantity'])} {UNITS_RU.get(it['unit'], '')}" if it["quantity"] is not None else ""
                approx = "≈ " if it["is_estimate"] and it["kcal"] is not None else ""
                meal_rows.append(
                    f"<tr><td>{escape(m['eaten_on'])} {text(m['eaten_time'])}</td>"
                    f"<td>{escape(MEAL_TYPES_RU.get(m['meal_type'], '') or '')}</td><td>{escape(it['name'])}</td>"
                    f"<td class=\"num\">{qty}</td><td class=\"num\">{approx}{fmt(it['kcal'])}</td></tr>")
        parts.append('<div class="card"><h2>Приёмы пищи</h2><table><tr><th>Когда</th><th>Приём</th><th>Продукт</th>'
                     '<th class="num">Количество</th><th class="num">Ккал</th></tr>' + "".join(meal_rows)
                     + "</table></div>")
    else:
        parts.append(f'<div class="card"><h2>Сводка по дням</h2>{empty()}</div>')
    return layout("Питание", "/nutrition", "".join(parts))


def alcohol_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    summary = alcohol.get_alcohol_summary(conn, date_from, date_to)
    entries = summary["entries"]
    per_day = {}
    for e in entries:
        if e["ethanol_g"] is not None:
            per_day[e["consumed_on"]] = per_day.get(e["consumed_on"], 0) + e["ethanol_g"]
    chart = charts.bar_chart(sorted(per_day.items()), "Чистый спирт по дням, г", "г")
    parts = [f'<div class="card"><h2>Чистый спирт по дням</h2>{period_selector("/alcohol", period)}'
             f'{chart or empty()}</div>']
    if entries:
        parts.append(
            '<div class="card"><h2>Итоги за период</h2><div class="stats">'
            f'<div class="stat"><span class="muted">Дней с алкоголем</span><b>{summary["days_with_alcohol"]}</b></div>'
            f'<div class="stat"><span class="muted">Записей</span><b>{summary["entries_count"]}</b></div>'
            f'<div class="stat"><span class="muted">Спирт, г</span><b>{fmt(summary["ethanol_g_total"])}</b></div>'
            f'<div class="stat"><span class="muted">Калории</span><b>{fmt(summary["kcal_total"])}</b></div>'
            "</div>"
            + (f'<p class="warn">Без объёма или крепости: {summary["entries_without_ethanol"]} — они не входят '
               "в сумму спирта.</p>" if summary["entries_without_ethanol"] else "")
            + "</div>"
        )
        rows = []
        for e in reversed(entries):
            kcal = fmt(e["kcal"])
            if e["kcal"] is not None and e["kcal_is_estimate"]:
                kcal = "≈ " + kcal
            rows.append(f"<tr><td>{escape(e['consumed_on'])} {text(e['consumed_time'])}</td>"
                        f"<td>{escape(e['drink'])}</td><td class=\"num\">{fmt(e['volume_ml'], unit='мл')}</td>"
                        f"<td class=\"num\">{fmt(e['abv_percent'], unit='%')}</td>"
                        f"<td class=\"num\">{fmt(e['ethanol_g'], unit='г')}</td><td class=\"num\">{kcal}</td>"
                        f"<td>{text(e['notes'])}</td></tr>")
        parts.append('<div class="card"><h2>История</h2><table><tr><th>Когда</th><th>Напиток</th>'
                     '<th class="num">Объём</th><th class="num">Крепость</th><th class="num">Спирт</th>'
                     '<th class="num">Ккал</th><th>Заметки</th></tr>' + "".join(rows) + "</table></div>")
    else:
        parts.append(f'<div class="card"><h2>История</h2>{empty()}</div>')
    return layout("Алкоголь", "/alcohol", "".join(parts))


def health_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    steps = health.daily_steps(conn, date_from, date_to)
    hr_days = health.daily_heart_rate(conn, date_from, date_to)
    sleep = health.daily_sleep(conn, date_from, date_to)
    parts = [f'<div class="card"><h2>Здоровье (Health Connect)</h2>{period_selector("/health", period)}'
             f'<p class="muted">Период: {escape(date_from)} — {escape(date_to)}. Даты местные: шаги и пульс — '
             "по времени записи, сон — по дате пробуждения. Когда был последний импорт — "
             f'<a href="/sync">Синхронизация</a>.</p></div>']

    if steps:
        total = sum(d["steps"] for d in steps)
        rows = []
        for d in reversed(steps):
            flags = []
            if d["overlap"]:
                flags.append("перекрытие интервалов учтено один раз")
            if d["sources"] > 1:
                flags.append(f"источников: {d['sources']}, взят наибольший")
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{fmt_int(d['steps'])}</td>"
                        f"<td class=\"num\">{d['intervals']}</td><td class=\"muted\">{escape('; '.join(flags))}</td></tr>")
        stats = (_stat("Дней с шагами", len(steps)) + _stat("Всего", fmt_int(total))
                 + _stat("В среднем за день", fmt_int(total / len(steps)))
                 + _stat("Максимум за день", fmt_int(max(d["steps"] for d in steps))))
        steps_html = (f'<div class="stats">{stats}</div>{_steps_chart(steps)}'
                      '<table><tr><th>Дата</th><th class="num">Шаги</th><th class="num">Интервалов</th><th></th></tr>'
                      + "".join(rows) + "</table>")
    else:
        steps_html = empty()
    parts.append(f'<div class="card"><h2>Шаги</h2>{steps_html}</div>')

    period_hr = health.heart_rate_period(hr_days)
    if period_hr:
        rows = "".join(
            f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['min_bpm']}</td>"
            f"<td class=\"num\">{fmt(d['avg_bpm'])}</td><td class=\"num\">{d['max_bpm']}</td>"
            f"<td class=\"num\">{fmt_int(d['samples'])}</td></tr>" for d in reversed(hr_days))
        stats = (_stat("Среднее", fmt(period_hr["avg_bpm"], unit="уд/мин"))
                 + _stat("Минимум", fmt(period_hr["min_bpm"], unit="уд/мин"))
                 + _stat("Максимум", fmt(period_hr["max_bpm"], unit="уд/мин"))
                 + _stat("Измерений", fmt_int(period_hr["samples"])))
        hr_html = (f'<div class="stats">{stats}</div>{_heart_chart(hr_days)}'
                   '<p class="muted">Учитываются отдельные измерения пульса, повтор одного измерения — один раз. '
                   "Среднее — по всем измерениям дня; часы меряют неравномерно, поэтому оно зависит от того, "
                   "когда были измерения.</p>"
                   '<table><tr><th>Дата</th><th class="num">Мин.</th><th class="num">Среднее</th>'
                   '<th class="num">Макс.</th><th class="num">Измерений</th></tr>' + rows + "</table>")
    else:
        hr_html = empty()
    parts.append(f'<div class="card"><h2>Пульс, уд/мин</h2>{hr_html}</div>')

    if sleep:
        avg = sum(d["in_bed_min"] for d in sleep) / len(sleep)
        staged = [d for d in sleep if d["asleep_min"] is not None]
        rows = []
        for d in reversed(sleep):
            note = f"стадии есть у {d['staged_sessions']} из {d['sessions']}" if d["staged_sessions"] < d["sessions"] else ""
            cells = "".join(f'<td class="num">{fmt_duration(d[k])}</td>'
                            for k in ("in_bed_min", "asleep_min", "deep_min", "light_min", "rem_min", "awake_min"))
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['sessions']}</td>{cells}"
                        f"<td class=\"muted\">{escape(note)}</td></tr>")
        stats = _stat("Суток со сном", len(sleep)) + _stat("Сон в среднем", fmt_duration(avg))
        if staged:
            stats += _stat("Во сне в среднем (по стадиям)",
                           fmt_duration(sum(d["asleep_min"] for d in staged) / len(staged)))
        sleep_html = (f'<div class="stats">{stats}</div>{_sleep_chart(sleep)}'
                      '<p class="muted">Сон относится к дате пробуждения; дневной сон входит в сумму суток. '
                      "«Сон» — общее время сессий, «во сне» — лёгкий, глубокий и быстрый сон по стадиям.</p>"
                      '<table><tr><th>Дата</th><th class="num">Сессий</th><th class="num">Сон</th>'
                      '<th class="num">Во сне</th><th class="num">Глубокий</th><th class="num">Лёгкий</th>'
                      '<th class="num">Быстрый</th><th class="num">Бодрствование</th><th></th></tr>'
                      + "".join(rows) + "</table>")
    else:
        sleep_html = empty()
    parts.append(f'<div class="card"><h2>Сон</h2>{sleep_html}</div>')
    return layout("Здоровье", "/health", "".join(parts))
