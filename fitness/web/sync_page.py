"""Страница «Синхронизация»: импорт Health Connect и входящая папка. Только чтение (fitness.health_status)."""

from html import escape

from . import pages

OUTCOME_RU = {"new": "новый, ещё не обрабатывался", "held": "ожидает", "busy": "копируется"}
KIND_RU = {"steps": "Шаги", "heart_rate": "Пульс", "sleep": "Сон"}


def _t(value):
    """Время UTC 'YYYY-MM-DDTHH:MM:SSZ' в виде '2026-10-12 02:44 UTC'."""
    if not value:
        return "—"
    return escape(value[:16].replace("T", " ") + " UTC") if "T" in value else escape(value)


def _cell(value):
    return escape(str(value)) if value is not None else "—"


def sync_page(status):
    parts = []
    attention = status.get("attention") or []
    if attention:
        items = "".join(f"<li>{escape(n)}</li>" for n in attention)
        parts.append(f'<div class="card"><h2 class="warn">Требует внимания</h2><ul>{items}</ul></div>')
    else:
        parts.append('<div class="card"><h2>Синхронизация в порядке</h2>'
                     f'<p class="muted">Проверено {_t(status["checked_at"])}.</p></div>')

    if status["database"]["ok"]:
        last = status["last_successful_import"]
        stats = (pages._stat("Последний импорт", _t(last["imported_at"]) if last else "ещё не было")
                 + pages._stat("Файлов импортировано", pages.fmt_int(status["imported_files"]))
                 + pages._stat("Неудачных попыток", pages.fmt_int(status["failed_attempts"])))
        rows = "".join(
            f"<tr><td>{KIND_RU[k]}</td><td class=\"num\">{pages.fmt_int(d['records'])}</td>"
            f"<td>{_cell(d['last_local_date'])}</td><td>{_t(d['last_record_end_utc'])}</td></tr>"
            for k, d in status["data"].items())
        phones = "".join(
            f"<tr><td>{escape(p['phone_id'])}</td><td class=\"num\">{p['last_sequence']}</td>"
            f"<td>{_t(p['last_exported_at'])}</td><td>{_t(p['last_imported_at'])}</td></tr>" for p in status["phones"])
        body = (f'<div class="stats">{stats}</div>'
                + (f'<p class="muted">Файл №{last["sequence"]}, создан на телефоне {_t(last["exported_at"])}.</p>'
                   if last else "")
                + '<h3>Последние данные в базе</h3><table><tr><th>Вид</th><th class="num">Записей</th>'
                  '<th>Последняя дата (местная)</th><th>Конец последней записи</th></tr>' + rows + "</table>")
        if phones:
            body += ('<h3>Телефоны</h3><table><tr><th>Телефон</th><th class="num">Последний файл №</th>'
                     "<th>Создан</th><th>Импортирован</th></tr>" + phones + "</table>")
        failures = status["recent_failures"]
        if failures:
            body += ('<h3>Последние неудачные попытки</h3><table><tr><th>Когда</th><th>Файл</th><th>Причина</th></tr>'
                     + "".join(f"<tr><td>{_t(f['attempted_at'])}</td><td>{escape(f['file'])}"
                               + ("" if f["after_last_success"] else ' <span class="muted">(до последнего успешного)</span>')
                               + f"</td><td>{escape(f['error'])}</td></tr>" for f in failures) + "</table>")
        parts.append(f'<div class="card"><h2>Импорт в базу</h2>{body}</div>')
    else:
        parts.append(f'<div class="card"><h2>База недоступна</h2><p>{escape(status["database"]["error"] or "")}</p></div>')

    inbox = status["inbox"]
    stats = (pages._stat("Ждут обработки", pages.fmt_int(inbox["waiting_files"]))
             + pages._stat("С ошибкой", pages.fmt_int(inbox["failed_files"]))
             + pages._stat("Обработано", pages.fmt_int(inbox["processed_files"])))
    check = inbox.get("last_check")
    body = (f'<div class="stats">{stats}</div><p class="muted">Входящая папка: {escape(inbox["folders"]["inbox"])}'
            + ("" if inbox["inbox_exists"] else " — ещё не создана, появится при первом запуске импорта")
            + f'. Последняя проверка: {_t(check["checked_at"]) if check else "ещё не было"}.</p>')
    sync = inbox.get("syncthing") or {}
    body += (f'<p class="muted">Папка Syncthing: {escape(inbox["folders"].get("syncthing", ""))}'
             + (f' — файлов {pages.fmt_int(sync["files"])}, ещё не передано на импорт '
                f'{pages.fmt_int(sync["not_handed_off"])}' if sync.get("exists") else " — не настроена")
             + ". Импорт её не меняет, только копирует новые файлы во входящую папку.</p>")
    if sync.get("old_setup"):
        body += ('<p class="warn">Syncthing всё ещё принимает файлы прямо во входящую папку: перенастройте его на '
                 "data\\health_syncthing (docs/HEALTH_SYNCTHING.md).</p>")
    if inbox["waiting"]:
        body += ("<h3>Во входящей папке</h3><table><tr><th>Файл</th><th>Состояние</th></tr>"
                 + "".join(f"<tr><td>{escape(w['file'])}</td><td>{escape(w['reason'] or OUTCOME_RU.get(w['outcome'], ''))}"
                           "</td></tr>" for w in inbox["waiting"]) + "</table>")
    if inbox["failed"]:
        body += ("<h3>Файлы с ошибкой (health_failed)</h3><table><tr><th>Файл</th><th>Когда</th><th>Причина</th></tr>"
                 + "".join(f"<tr><td>{escape(f['file'])}</td><td>{_t(f['failed_at'])}</td><td>{escape(f['error'] or '')}"
                           "</td></tr>" for f in inbox["failed"]) + "</table>"
                 '<p class="muted">Причина полностью — в файле &lt;имя&gt;.error.txt рядом. После исправления: '
                 "health_import_inbox.bat --retry-failed.</p>")
    last_run = inbox.get("last_run")
    if last_run:
        summary = ", ".join(f"{escape(k)}: {v}" for k, v in (last_run.get("summary") or {}).items() if v)
        body += (f"<h3>Последний запуск с файлами</h3><p>{_t(last_run.get('finished_at'))}"
                 + (f" — {summary}" if summary else "") + "</p>")
        if last_run.get("error"):
            body += f'<p class="warn">Остановлен: {escape(last_run["error"].get("message") or "")}</p>'
    body += ('<p class="muted">Импорт новых файлов: health_import_inbox.bat (один раз) или '
             "health_import_inbox.bat --watch (наблюдение). Панель файлы не обрабатывает.</p>")
    parts.append(f'<div class="card"><h2>Входящая папка</h2>{body}</div>')
    return pages.layout("Синхронизация", "/sync", "".join(parts))
