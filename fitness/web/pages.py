"""HTML-страницы панели. Только чтение: используются готовые функции get_* поверх read-only соединения."""

from contextvars import ContextVar
from datetime import date, timedelta
from html import escape
from urllib.parse import urlencode

from .. import alcohol, i18n, measurements, nutrition, workouts
from ..i18n import t
from ..validation import MAX_RANGE_DAYS
from . import charts, health

# Код периода -> ключ подписи. Коды идут в адрес страницы и не переводятся.
PERIODS = {"30": "period.30", "90": "period.90", "365": "period.365", "all": "period.all"}
DEFAULT_PERIOD = "90"

NAV = [
    ("/", "nav.overview"),
    ("/measurements", "nav.measurements"),
    ("/workouts", "nav.workouts"),
    ("/nutrition", "nav.nutrition"),
    ("/alcohol", "nav.alcohol"),
    ("/health", "nav.health"),
    ("/photos", "nav.photos"),
    ("/sync", "nav.sync"),
]

# Форма переключения языка в шапке: (секрет формы, адрес возврата) текущего запроса. Без неё (прямой вызов
# функций страницы) переключатель не выводится.
language_form = ContextVar("language_form", default=None)

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
.lang{float:right;display:flex;gap:4px;margin:0 0 0 12px}.lang button{font:inherit;font-size:13px;padding:1px 9px;
border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--muted);cursor:pointer}
.lang button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff;cursor:default}
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
    """Целое с разделителем разрядов: 12,345 (en) или 12 345 (ru, неразрывный пробел)."""
    if value is None:
        return '<span class="muted">—</span>'
    return i18n.group_thousands(value)


def fmt_duration(minutes):
    """Минуты -> «7 h 05 min» / «7 ч 05 мин»."""
    if minutes is None:
        return '<span class="muted">—</span>'
    hours, mins = divmod(round(minutes), 60)
    return t("duration.hm", h=hours, m=f"{mins:02d}") if hours else t("duration.m", m=mins)


def u(name):
    """Единица измерения на языке интерфейса: u("kg") -> «kg» / «кг»."""
    return t("unit." + name)


def code_label(group, code):
    """Подпись кода из базы (тип тренировки, приём пищи, единица): перевод или сам код, если перевода нет."""
    if not code:
        return ""
    key = f"{group}.{code}"
    label = t(key)
    return code if label == key else label


def measurement_name(code, name_ru):
    """Название вида замера: по-русски — из базы (name_ru), по-английски — из словаря по коду."""
    if i18n.get_language() == "ru":
        return name_ru
    key = "measure." + code
    label = t(key)
    return name_ru if label == key else label


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
    for key, label_key in PERIODS.items():
        cls = ' class="active"' if key == period else ""
        items.append(f'<a{cls} href="{link(path, period=key, **params)}">{escape(t(label_key))}</a>')
    return f'<div class="periods">{"".join(items)}</div>'


def empty(message=None):
    return f'<p class="empty">{escape(message or t("empty.period"))}</p>'


def language_switch():
    """Переключатель EN | RU: форма POST /settings/language с секретом формы. Пусто, если формы нет."""
    form = language_form.get()
    if form is None:
        return ""
    csrf, back = form
    current = i18n.get_language()
    buttons = "".join(
        f'<button type="submit" name="lang" value="{code}" lang="{code}" aria-pressed="{str(code == current).lower()}"'
        f' title="{i18n.LANGUAGE_NAMES[code]}">{label}</button>'
        for code, label in i18n.LANGUAGES.items())
    return (f'<form class="lang" method="post" action="/settings/language" aria-label="{escape(t("lang.label"))}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            f'<input type="hidden" name="next" value="{escape(back)}">{buttons}</form>')


def layout(title, active, body):
    links = []
    for href, label_key in NAV:
        cls = ' class="active"' if href == active else ""
        links.append(f'<a href="{href}"{cls}>{escape(t(label_key))}</a>')
    nav = "".join(links)
    return (
        f'<!doctype html><html lang="{i18n.get_language()}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} — FITNESS Tracker</title><style>{CSS}</style></head><body>"
        f"<header>{language_switch()}<h1>FITNESS Tracker</h1><nav>{nav}</nav></header><main>{body}</main>"
        f"<footer>{escape(t('footer'))}</footer></body></html>"
    )


def message_page(title, heading, message="", active="", back=None):
    """Простая страница с заголовком и текстом (ошибки, отказ в запросе)."""
    body = f'<div class="card"><h2>{escape(heading)}</h2>'
    if message:
        body += f"<p>{escape(message)}</p>"
    if back:
        body += f'<p><a href="{escape(back[0])}">{escape(back[1])}</a></p>'
    return layout(title, active, body + "</div>")


def unavailable_page(message):
    return message_page(t("db_unavailable.title"), t("db_unavailable.title"), message, "/")


def not_found_page():
    return message_page(t("not_found.title"), t("not_found.heading"))


# ---------- Страницы ----------

def overview_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    counts = {
        "overview.count.measurements": conn.execute("SELECT COUNT(*) FROM measurement_sessions").fetchone()[0],
        "overview.count.workouts": conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0],
        "overview.count.meals": conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0],
        "overview.count.alcohol": conn.execute("SELECT COUNT(*) FROM alcohol_entries").fetchone()[0],
        "overview.count.notes": conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        "overview.count.photos": conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
    }
    stats = "".join(_stat(t(k), v) for k, v in counts.items())
    parts = [f'<div class="card"><h2>{t("overview.totals")}</h2><div class="stats">{stats}</div></div>']

    health_counts = health.counts(conn)
    has_health = sum(health_counts.values()) > 0
    if has_health:
        hstats = "".join(_stat(t(label), fmt_int(health_counts[key])) for key, label in (
            ("steps", "overview.health.steps"), ("heart_rate", "overview.health.heart_rate"),
            ("sleep", "overview.health.sleep")))
        parts.append(f'<div class="card"><h2>Health Connect</h2><div class="stats">{hstats}</div></div>')

    if sum(counts.values()) == 0:
        if has_health:
            parts.extend(_health_overview(conn, period, date_from, date_to, with_selector=True))
        else:
            parts.append(f'<div class="card"><h2>{escape(t("overview.empty.heading"))}</h2>'
                         f'<p>{escape(t("overview.empty.text"))}</p></div>')
        return layout(t("nav.overview"), "/", "".join(parts))

    last_weight = conn.execute(
        "SELECT measured_on, weight_kg FROM measurement_sessions WHERE weight_kg IS NOT NULL"
        " ORDER BY measured_on DESC, measured_time DESC, id DESC LIMIT 1").fetchone()
    last_workout = conn.execute(
        "SELECT workout_date, title FROM workouts ORDER BY workout_date DESC, started_time DESC, id DESC LIMIT 1"
    ).fetchone()
    latest = []
    if last_weight:
        latest.append(f"<li>{escape(t('overview.last_weight'))} <b>{fmt(last_weight['weight_kg'], unit=u('kg'))}</b> "
                      f"({escape(last_weight['measured_on'])})</li>")
    if last_workout:
        latest.append(f"<li>{escape(t('overview.last_workout'))} <b>{escape(last_workout['title'])}</b> "
                      f"({escape(last_workout['workout_date'])})</li>")
    if latest:
        parts.append(f'<div class="card"><h2>{t("overview.latest")}</h2><ul>{"".join(latest)}</ul></div>')

    weights = measurements.get_measurements(conn, date_from, date_to, "weight")["records"]
    chart = charts.line_chart([(r["measured_on"], r["weight_kg"]) for r in weights],
                              t("chart.weight", unit=u("kg")), u("kg"))
    parts.append(f'<div class="card"><h2>{t("overview.weight_period")}</h2>{period_selector("/", period)}'
                 f'{chart or empty()}</div>')
    if has_health:
        parts.extend(_health_overview(conn, period, date_from, date_to, with_selector=False))
    return layout(t("nav.overview"), "/", "".join(parts))


def _stat(label, value):
    return f'<div class="stat"><span class="muted">{escape(label)}</span><b>{value}</b></div>'


def _steps_chart(days):
    return charts.bar_chart([(d["date"], d["steps"]) for d in days], t("chart.steps"), u("steps"))


def _heart_chart(days):
    return charts.range_chart([(d["date"], d["min_bpm"], d["avg_bpm"], d["max_bpm"]) for d in days],
                              t("chart.heart_rate", unit=u("bpm")), u("bpm"))


def _sleep_chart(days):
    return charts.bar_chart([(d["date"], round(d["in_bed_min"] / 60, 2)) for d in days],
                            t("chart.sleep", unit=u("h")), u("h"))


def _health_overview(conn, period, date_from, date_to, with_selector):
    """Карточки шагов, пульса и сна для обзора."""
    steps = health.daily_steps(conn, date_from, date_to)
    hr_days = health.daily_heart_rate(conn, date_from, date_to)
    sleep = health.daily_sleep(conn, date_from, date_to)
    selector = period_selector("/", period) if with_selector else ""

    if steps:
        total = sum(d["steps"] for d in steps)
        last = steps[-1]
        stats = (_stat(t("stat.daily_average"), fmt_int(total / len(steps)))
                 + _stat(t("stat.period_total"), fmt_int(total))
                 + _stat(t("stat.last_day", date=last["date"]), fmt_int(last["steps"])))
        steps_html = f'<div class="stats">{stats}</div>{_steps_chart(steps)}'
    else:
        steps_html = empty()

    period_hr = health.heart_rate_period(hr_days)
    if period_hr:
        last = hr_days[-1]
        stats = (_stat(t("stat.period_average"), fmt(period_hr["avg_bpm"], unit=u("bpm")))
                 + _stat(t("stat.period_min_max"), f"{period_hr['min_bpm']}–{period_hr['max_bpm']}")
                 + _stat(t("stat.last_day", date=last["date"]),
                         t("stat.hr_day", low=last["min_bpm"], high=last["max_bpm"], avg=fmt(last["avg_bpm"]))))
        hr_html = f'<div class="stats">{stats}</div>{_heart_chart(hr_days)}'
    else:
        hr_html = empty()

    if sleep:
        avg = sum(d["in_bed_min"] for d in sleep) / len(sleep)
        last = sleep[-1]
        stats = (_stat(t("stat.sleep_daily_average"), fmt_duration(avg))
                 + _stat(t("stat.last_night", date=last["date"]), fmt_duration(last["in_bed_min"]))
                 + _stat(t("stat.asleep_staged"), fmt_duration(last["asleep_min"])))
        sleep_html = f'<div class="stats">{stats}</div>{_sleep_chart(sleep)}'
    else:
        sleep_html = empty()

    more = f'<p><a href="{link("/health", period=period)}">{escape(t("overview.health_more"))}</a></p>'
    return [f'<div class="card"><h2>{t("health.steps")}</h2>{selector}{steps_html}</div>',
            f'<div class="card"><h2>{escape(t("health.heart_rate", unit=u("bpm")))}</h2>{hr_html}</div>',
            f'<div class="card"><h2>{t("health.sleep")}</h2>{sleep_html}{more}</div>']


def _measure_unit(unit):
    return u("cm") if unit == "cm" else "%"


def measurements_page(conn, period, type_code=None):
    date_from, date_to = period_bounds(conn, period)
    types = measurements.measurement_types(conn)
    if type_code not in types:
        type_code = None
    data = measurements.get_measurements(conn, date_from, date_to)["records"]

    chips = ['<a{} href="{}">{}</a>'.format(' class="active"' if type_code is None else "",
                                            link("/measurements", period=period), escape(t("measurements.weight")))]
    for code, ty in types.items():
        chips.append('<a{} href="{}">{}</a>'.format(' class="active"' if code == type_code else "",
                                                     link("/measurements", period=period, type=code),
                                                     escape(measurement_name(code, ty["name_ru"]))))
    if type_code is None:
        points = [(r["measured_on"], r["weight_kg"]) for r in data]
        title, unit = t("measurements.weight"), u("kg")
    else:
        ty = types[type_code]
        unit = _measure_unit(ty["unit"])
        points = [(r["measured_on"], m["value"]) for r in data for m in r["measurements"] if m["type"] == type_code]
        title = measurement_name(type_code, ty["name_ru"])
    chart = charts.line_chart(points, f"{title}, {unit}", unit)

    parts = [f'<div class="card"><h2>{escape(title)}</h2>{period_selector("/measurements", period, type=type_code)}'
             f'<div class="chips">{"".join(chips)}</div>{chart or empty()}</div>']

    if data:
        rows = []
        for r in reversed(data):
            ms = ", ".join(f"{escape(measurement_name(m['type'], m['name_ru']))}: "
                           f"{fmt(m['value'], unit=_measure_unit(m['unit']))}" for m in r["measurements"])
            photos = len(r["photos"])
            photo_note = " · " + escape(t("measurements.photos", n=photos)) if photos else ""
            rows.append(f"<tr><td>{escape(r['measured_on'])} {text(r['measured_time'])}</td>"
                        f"<td class=\"num\">{fmt(r['weight_kg'], unit=u('kg'))}</td><td>{ms or ''}</td>"
                        f"<td>{text(r['notes'])}{photo_note}</td></tr>")
        table = (f"<table><tr><th>{t('col.date')}</th><th class=\"num\">{t('col.weight')}</th>"
                 f"<th>{t('col.measurements')}</th><th>{t('col.notes')}</th></tr>" + "".join(rows) + "</table>")
    else:
        table = empty()
    parts.append(f'<div class="card"><h2>{t("card.history")}</h2>{table}</div>')
    return layout(t("nav.measurements"), "/measurements", "".join(parts))


def _set_line(s):
    bits = []
    if s["weight_kg"] is not None:
        bits.append(fmt(s["weight_kg"], unit=u("kg")))
    if s["reps"] is not None:
        bits.append(f"× {s['reps']}")
    if s["duration_sec"] is not None:
        bits.append(fmt(s["duration_sec"], unit=u("s")))
    if s["distance_m"] is not None:
        bits.append(fmt(s["distance_m"], unit=u("m")))
    if s["rir"] is not None:
        bits.append(f"RIR {fmt(s['rir'])}")
    line = " ".join(bits)
    if s["is_warmup"]:
        line += f' <span class="muted">({escape(t("workouts.warmup"))})</span>'
    if s["notes"]:
        line += f' <span class="muted">— {escape(s["notes"])}</span>'
    return line


def workouts_page(conn, period, exercise=None):
    date_from, date_to = period_bounds(conn, period)
    names = [e["name"] for e in workouts.list_exercises(conn)]
    if exercise not in names:
        exercise = None
    data = workouts.get_workouts(conn, date_from, date_to, exercise=exercise)["records"]

    chips = ['<a{} href="{}">{}</a>'.format(' class="active"' if exercise is None else "",
                                            link("/workouts", period=period), escape(t("workouts.all")))]
    for name in names:
        chips.append('<a{} href="{}">{}</a>'.format(' class="active"' if name == exercise else "",
                                                     link("/workouts", period=period, exercise=name), escape(name)))
    parts = [f'<div class="card"><h2>{t("nav.workouts")}</h2>{period_selector("/workouts", period, exercise=exercise)}'
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
        chart = charts.line_chart(sorted(best.items()), t("chart.best_weight", exercise=exercise, unit=u("kg")), u("kg"))
        parts.append(f'<div class="card"><h2>{escape(t("workouts.best_weight", exercise=exercise))}</h2>'
                     f'{chart or empty(t("workouts.no_weighted_sets"))}</div>')

    if not data:
        parts.append(f'<div class="card">{empty()}</div>')
    else:
        items = []
        for w in reversed(data):
            meta = [code_label("workout_type", w["workout_type"])]
            if w["started_time"]:
                meta.append(w["started_time"])
            if w["duration_min"] is not None:
                meta.append(f"{fmt(w['duration_min'])} {u('min')}")
            exs = []
            for ex in w["exercises"]:
                if exercise and ex["name"] != exercise:
                    continue
                sets = "".join(f"<li>{_set_line(s)}</li>" for s in ex["sets"])
                extra = ""
                if ex["discomfort"]:
                    extra += f'<div class="warn">{escape(t("workouts.discomfort"))} {escape(ex["discomfort"])}</div>'
                if ex["notes"]:
                    extra += f'<div class="muted">{escape(ex["notes"])}</div>'
                exs.append(f"<li><b>{escape(ex['name'])}</b>{extra}<ol>{sets}</ol></li>")
            notes = f'<div class="muted">{escape(w["notes"])}</div>' if w["notes"] else ""
            items.append(f'<div class="workout"><b>{escape(w["workout_date"])}</b> — {escape(w["title"])} '
                         f'<span class="muted">({escape(", ".join(meta))})</span>{notes}<ul>{"".join(exs)}</ul></div>')
        parts.append(f'<div class="card"><h2>{t("card.history")}</h2>{"".join(items)}</div>')
    return layout(t("nav.workouts"), "/workouts", "".join(parts))


def nutrition_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    summary = nutrition.get_nutrition_summary(conn, date_from, date_to, include_meals=True)
    days = summary["days"]
    chart = charts.bar_chart([(d["date"], d["kcal"]) for d in days], t("nutrition.kcal_by_day"), u("kcal"))
    parts = [f'<div class="card"><h2>{t("nutrition.kcal_by_day")}</h2>{period_selector("/nutrition", period)}'
             f'{chart or empty()}</div>']
    if days:
        totals = summary["totals"]
        avg = summary["average_kcal_per_logged_day"]
        rows = []
        for d in reversed(days):
            flags = []
            if not d["totals_complete"]:
                flags.append(f'<span class="warn">{escape(t("nutrition.without_kcal", n=d["items_without_kcal"]))}</span>')
            if d["items_estimated"]:
                flags.append(escape(t("nutrition.estimated", n=d["items_estimated"], total=d["items"])))
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['meals']}</td>"
                        f"<td class=\"num\">{fmt(d['kcal'])}</td><td class=\"num\">{fmt(d['protein_g'])}</td>"
                        f"<td class=\"num\">{fmt(d['fat_g'])}</td><td class=\"num\">{fmt(d['carbs_g'])}</td>"
                        f"<td>{' · '.join(flags)}</td></tr>")
        parts.append(
            f'<div class="card"><h2>{t("nutrition.daily_summary")}</h2>'
            # fmt() уже возвращает готовый HTML, тексты словаря без разметки — повторное экранирование не нужно.
            + "<p>" + t("nutrition.period_totals", kcal=fmt(totals["kcal"], unit=u("kcal")),
                        protein=fmt(totals["protein_g"], unit=u("g")), fat=fmt(totals["fat_g"], unit=u("g")),
                        carbs=fmt(totals["carbs_g"], unit=u("g")), avg=fmt(avg, unit=u("kcal"))) + "</p>"
            f'<p class="muted">{escape(t("nutrition.totals_note"))}</p>'
            f'<table><tr><th>{t("col.date")}</th><th class="num">{t("col.meals")}</th><th class="num">{t("col.kcal")}</th>'
            f'<th class="num">{t("col.protein")}</th><th class="num">{t("col.fat")}</th><th class="num">{t("col.carbs")}</th>'
            f'<th>{t("col.completeness")}</th></tr>' + "".join(rows) + "</table></div>"
        )
        meal_rows = []
        for m in reversed(summary["meals"]):
            for it in m["items"]:
                qty = f"{fmt(it['quantity'])} {code_label('unit', it['unit'])}" if it["quantity"] is not None else ""
                approx = "≈ " if it["is_estimate"] and it["kcal"] is not None else ""
                meal_rows.append(
                    f"<tr><td>{escape(m['eaten_on'])} {text(m['eaten_time'])}</td>"
                    f"<td>{escape(code_label('meal_type', m['meal_type']))}</td><td>{escape(it['name'])}</td>"
                    f"<td class=\"num\">{qty}</td><td class=\"num\">{approx}{fmt(it['kcal'])}</td></tr>")
        parts.append(f'<div class="card"><h2>{t("nutrition.meals")}</h2><table><tr><th>{t("col.when")}</th>'
                     f'<th>{t("col.meal")}</th><th>{t("col.food")}</th><th class="num">{t("col.quantity")}</th>'
                     f'<th class="num">{t("col.kcal")}</th></tr>' + "".join(meal_rows) + "</table></div>")
    else:
        parts.append(f'<div class="card"><h2>{t("nutrition.daily_summary")}</h2>{empty()}</div>')
    return layout(t("nav.nutrition"), "/nutrition", "".join(parts))


def alcohol_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    summary = alcohol.get_alcohol_summary(conn, date_from, date_to)
    entries = summary["entries"]
    per_day = {}
    for e in entries:
        if e["ethanol_g"] is not None:
            per_day[e["consumed_on"]] = per_day.get(e["consumed_on"], 0) + e["ethanol_g"]
    chart = charts.bar_chart(sorted(per_day.items()), t("chart.ethanol", unit=u("g")), u("g"))
    parts = [f'<div class="card"><h2>{t("alcohol.ethanol_by_day")}</h2>{period_selector("/alcohol", period)}'
             f'{chart or empty()}</div>']
    if entries:
        parts.append(
            f'<div class="card"><h2>{t("alcohol.period_totals")}</h2><div class="stats">'
            + _stat(t("alcohol.days"), summary["days_with_alcohol"])
            + _stat(t("alcohol.entries"), summary["entries_count"])
            + _stat(t("alcohol.ethanol_total", unit=u("g")), fmt(summary["ethanol_g_total"]))
            + _stat(t("alcohol.kcal"), fmt(summary["kcal_total"]))
            + "</div>"
            + (f'<p class="warn">{escape(t("alcohol.without_ethanol", n=summary["entries_without_ethanol"]))}</p>'
               if summary["entries_without_ethanol"] else "")
            + "</div>"
        )
        rows = []
        for e in reversed(entries):
            kcal = fmt(e["kcal"])
            if e["kcal"] is not None and e["kcal_is_estimate"]:
                kcal = "≈ " + kcal
            rows.append(f"<tr><td>{escape(e['consumed_on'])} {text(e['consumed_time'])}</td>"
                        f"<td>{escape(e['drink'])}</td><td class=\"num\">{fmt(e['volume_ml'], unit=u('ml'))}</td>"
                        f"<td class=\"num\">{fmt(e['abv_percent'], unit='%')}</td>"
                        f"<td class=\"num\">{fmt(e['ethanol_g'], unit=u('g'))}</td><td class=\"num\">{kcal}</td>"
                        f"<td>{text(e['notes'])}</td></tr>")
        parts.append(f'<div class="card"><h2>{t("card.history")}</h2><table><tr><th>{t("col.when")}</th>'
                     f'<th>{t("col.drink")}</th><th class="num">{t("col.volume")}</th><th class="num">{t("col.abv")}</th>'
                     f'<th class="num">{t("col.ethanol")}</th><th class="num">{t("col.kcal")}</th>'
                     f'<th>{t("col.notes")}</th></tr>' + "".join(rows) + "</table></div>")
    else:
        parts.append(f'<div class="card"><h2>{t("card.history")}</h2>{empty()}</div>')
    return layout(t("nav.alcohol"), "/alcohol", "".join(parts))


def health_page(conn, period):
    date_from, date_to = period_bounds(conn, period)
    steps = health.daily_steps(conn, date_from, date_to)
    hr_days = health.daily_heart_rate(conn, date_from, date_to)
    sleep = health.daily_sleep(conn, date_from, date_to)
    parts = [f'<div class="card"><h2>{t("health.title")}</h2>{period_selector("/health", period)}'
             f'<p class="muted">{escape(t("health.period_note", start=date_from, end=date_to))} '
             f'<a href="/sync">{escape(t("nav.sync"))}</a>.</p></div>']

    if steps:
        total = sum(d["steps"] for d in steps)
        rows = []
        for d in reversed(steps):
            flags = []
            if d["overlap"]:
                flags.append(t("health.steps.overlap"))
            if d["sources"] > 1:
                flags.append(t("health.steps.sources", n=d["sources"]))
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{fmt_int(d['steps'])}</td>"
                        f"<td class=\"num\">{d['intervals']}</td><td class=\"muted\">{escape('; '.join(flags))}</td></tr>")
        stats = (_stat(t("health.steps.days"), len(steps)) + _stat(t("stat.total"), fmt_int(total))
                 + _stat(t("stat.daily_average"), fmt_int(total / len(steps)))
                 + _stat(t("health.steps.max_day"), fmt_int(max(d["steps"] for d in steps))))
        steps_html = (f'<div class="stats">{stats}</div>{_steps_chart(steps)}'
                      f'<table><tr><th>{t("col.date")}</th><th class="num">{t("col.steps")}</th>'
                      f'<th class="num">{t("col.intervals")}</th><th></th></tr>' + "".join(rows) + "</table>")
    else:
        steps_html = empty()
    parts.append(f'<div class="card"><h2>{t("health.steps")}</h2>{steps_html}</div>')

    period_hr = health.heart_rate_period(hr_days)
    if period_hr:
        rows = "".join(
            f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['min_bpm']}</td>"
            f"<td class=\"num\">{fmt(d['avg_bpm'])}</td><td class=\"num\">{d['max_bpm']}</td>"
            f"<td class=\"num\">{fmt_int(d['samples'])}</td></tr>" for d in reversed(hr_days))
        stats = (_stat(t("stat.average"), fmt(period_hr["avg_bpm"], unit=u("bpm")))
                 + _stat(t("stat.minimum"), fmt(period_hr["min_bpm"], unit=u("bpm")))
                 + _stat(t("stat.maximum"), fmt(period_hr["max_bpm"], unit=u("bpm")))
                 + _stat(t("health.hr.samples"), fmt_int(period_hr["samples"])))
        hr_html = (f'<div class="stats">{stats}</div>{_heart_chart(hr_days)}'
                   f'<p class="muted">{escape(t("health.hr.note"))}</p>'
                   f'<table><tr><th>{t("col.date")}</th><th class="num">{t("col.min")}</th>'
                   f'<th class="num">{t("col.average")}</th><th class="num">{t("col.max")}</th>'
                   f'<th class="num">{t("col.samples")}</th></tr>' + rows + "</table>")
    else:
        hr_html = empty()
    parts.append(f'<div class="card"><h2>{escape(t("health.heart_rate", unit=u("bpm")))}</h2>{hr_html}</div>')

    if sleep:
        avg = sum(d["in_bed_min"] for d in sleep) / len(sleep)
        staged = [d for d in sleep if d["asleep_min"] is not None]
        rows = []
        for d in reversed(sleep):
            note = (t("health.sleep.staged_of", staged=d["staged_sessions"], total=d["sessions"])
                    if d["staged_sessions"] < d["sessions"] else "")
            cells = "".join(f'<td class="num">{fmt_duration(d[k])}</td>'
                            for k in ("in_bed_min", "asleep_min", "deep_min", "light_min", "rem_min", "awake_min"))
            rows.append(f"<tr><td>{escape(d['date'])}</td><td class=\"num\">{d['sessions']}</td>{cells}"
                        f"<td class=\"muted\">{escape(note)}</td></tr>")
        stats = _stat(t("health.sleep.days"), len(sleep)) + _stat(t("health.sleep.average"), fmt_duration(avg))
        if staged:
            stats += _stat(t("health.sleep.asleep_average"),
                           fmt_duration(sum(d["asleep_min"] for d in staged) / len(staged)))
        sleep_html = (f'<div class="stats">{stats}</div>{_sleep_chart(sleep)}'
                      f'<p class="muted">{escape(t("health.sleep.note"))}</p>'
                      f'<table><tr><th>{t("col.date")}</th><th class="num">{t("col.sessions")}</th>'
                      f'<th class="num">{t("col.sleep")}</th><th class="num">{t("col.asleep")}</th>'
                      f'<th class="num">{t("col.deep")}</th><th class="num">{t("col.light")}</th>'
                      f'<th class="num">{t("col.rem")}</th><th class="num">{t("col.awake")}</th><th></th></tr>'
                      + "".join(rows) + "</table>")
    else:
        sleep_html = empty()
    parts.append(f'<div class="card"><h2>{t("health.sleep")}</h2>{sleep_html}</div>')
    return layout(t("nav.health"), "/health", "".join(parts))
