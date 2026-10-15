"""Страница «Синхронизация»: импорт Health Connect и входящая папка. Только чтение (fitness.health_status).

Подписи страницы переводятся; тексты предупреждений и причин ошибок приходят из fitness.health_status и
журнала импорта как есть.
"""

from html import escape

from ..i18n import t
from . import pages

OUTCOMES = ("new", "held", "busy")     # состояние файла во входящей папке -> ключ sync.outcome.<код>


def _t(value):
    """Время UTC 'YYYY-MM-DDTHH:MM:SSZ' в виде '2026-10-12 02:44 UTC'."""
    if not value:
        return "—"
    return escape(value[:16].replace("T", " ") + " UTC") if "T" in value else escape(value)


def _cell(value):
    return escape(str(value)) if value is not None else "—"


def _outcome(code):
    return t("sync.outcome." + code) if code in OUTCOMES else ""


def sync_page(status):
    parts = []
    attention = status.get("attention") or []
    if attention:
        items = "".join(f"<li>{escape(n)}</li>" for n in attention)
        parts.append(f'<div class="card"><h2 class="warn">{t("sync.attention")}</h2><ul>{items}</ul></div>')
    else:
        parts.append(f'<div class="card"><h2>{t("sync.ok")}</h2>'
                     f'<p class="muted">{t("sync.checked_at", at=_t(status["checked_at"]))}</p></div>')

    if status["database"]["ok"]:
        last = status["last_successful_import"]
        stats = (pages._stat(t("sync.last_import"), _t(last["imported_at"]) if last else t("sync.never"))
                 + pages._stat(t("sync.files_imported"), pages.fmt_int(status["imported_files"]))
                 + pages._stat(t("sync.failed_attempts"), pages.fmt_int(status["failed_attempts"])))
        rows = "".join(
            f"<tr><td>{t('sync.kind.' + k)}</td><td class=\"num\">{pages.fmt_int(d['records'])}</td>"
            f"<td>{_cell(d['last_local_date'])}</td><td>{_t(d['last_record_end_utc'])}</td></tr>"
            for k, d in status["data"].items())
        phones = "".join(
            f"<tr><td>{escape(p['phone_id'])}</td><td class=\"num\">{p['last_sequence']}</td>"
            f"<td>{_t(p['last_exported_at'])}</td><td>{_t(p['last_imported_at'])}</td></tr>" for p in status["phones"])
        body = (f'<div class="stats">{stats}</div>'
                + (f'<p class="muted">{t("sync.last_file", n=last["sequence"], at=_t(last["exported_at"]))}</p>'
                   if last else "")
                + f'<h3>{t("sync.latest_data")}</h3><table><tr><th>{t("sync.col.kind")}</th>'
                  f'<th class="num">{t("sync.col.records")}</th><th>{t("sync.col.last_local_date")}</th>'
                  f'<th>{t("sync.col.last_record_end")}</th></tr>' + rows + "</table>")
        if phones:
            body += (f'<h3>{t("sync.phones")}</h3><table><tr><th>{t("sync.col.phone")}</th>'
                     f'<th class="num">{t("sync.col.last_file")}</th><th>{t("sync.col.created")}</th>'
                     f'<th>{t("sync.col.imported")}</th></tr>' + phones + "</table>")
        failures = status["recent_failures"]
        if failures:
            body += (f'<h3>{t("sync.recent_failures")}</h3><table><tr><th>{t("col.when")}</th>'
                     f'<th>{t("sync.col.file")}</th><th>{t("sync.col.reason")}</th></tr>'
                     + "".join(f"<tr><td>{_t(f['attempted_at'])}</td><td>{escape(f['file'])}"
                               + ("" if f["after_last_success"]
                                  else f' <span class="muted">({t("sync.before_last_success")})</span>')
                               + f"</td><td>{escape(f['error'])}</td></tr>" for f in failures) + "</table>")
        parts.append(f'<div class="card"><h2>{t("sync.import")}</h2>{body}</div>')
    else:
        parts.append(f'<div class="card"><h2>{t("db_unavailable.title")}</h2>'
                     f'<p>{escape(status["database"]["error"] or "")}</p></div>')

    inbox = status["inbox"]
    stats = (pages._stat(t("sync.waiting"), pages.fmt_int(inbox["waiting_files"]))
             + pages._stat(t("sync.with_errors"), pages.fmt_int(inbox["failed_files"]))
             + pages._stat(t("sync.processed"), pages.fmt_int(inbox["processed_files"])))
    check = inbox.get("last_check")
    body = (f'<div class="stats">{stats}</div><p class="muted">'
            + escape(t("sync.inbox_folder", path=inbox["folders"]["inbox"]))
            + ("" if inbox["inbox_exists"] else escape(t("sync.inbox_missing")))
            + escape(t("sync.inbox_last_check")) + (_t(check["checked_at"]) if check else t("sync.never"))
            + ".</p>")
    sync = inbox.get("syncthing") or {}
    body += ('<p class="muted">' + escape(t("sync.syncthing_folder", path=inbox["folders"].get("syncthing", "")))
             + (t("sync.syncthing_files", files=pages.fmt_int(sync["files"]),
                  pending=pages.fmt_int(sync["not_handed_off"]))
                if sync.get("exists") else escape(t("sync.syncthing_missing")))
             + escape(t("sync.syncthing_note")) + "</p>")
    if sync.get("old_setup"):
        body += f'<p class="warn">{escape(t("sync.syncthing_old_setup"))}</p>'
    if inbox["waiting"]:
        body += (f'<h3>{t("sync.in_inbox")}</h3><table><tr><th>{t("sync.col.file")}</th>'
                 f'<th>{t("sync.col.state")}</th></tr>'
                 + "".join(f"<tr><td>{escape(w['file'])}</td><td>{escape(w['reason'] or _outcome(w['outcome']))}"
                           "</td></tr>" for w in inbox["waiting"]) + "</table>")
    if inbox["failed"]:
        body += (f'<h3>{t("sync.failed_files")}</h3><table><tr><th>{t("sync.col.file")}</th>'
                 f'<th>{t("col.when")}</th><th>{t("sync.col.reason")}</th></tr>'
                 + "".join(f"<tr><td>{escape(f['file'])}</td><td>{_t(f['failed_at'])}</td><td>{escape(f['error'] or '')}"
                           "</td></tr>" for f in inbox["failed"]) + "</table>"
                 f'<p class="muted">{escape(t("sync.failed_note"))}</p>')
    last_run = inbox.get("last_run")
    if last_run:
        summary = ", ".join(f"{escape(k)}: {v}" for k, v in (last_run.get("summary") or {}).items() if v)
        body += (f"<h3>{t('sync.last_run')}</h3><p>{_t(last_run.get('finished_at'))}"
                 + (f" — {summary}" if summary else "") + "</p>")
        if last_run.get("error"):
            body += (f'<p class="warn">{escape(t("sync.stopped"))} '
                     f'{escape(last_run["error"].get("message") or "")}</p>')
    body += f'<p class="muted">{escape(t("sync.how_to_import"))}</p>'
    parts.append(f'<div class="card"><h2>{t("sync.inbox")}</h2>{body}</div>')
    return pages.layout(t("nav.sync"), "/sync", "".join(parts))
