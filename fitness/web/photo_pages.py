"""Раздел «Фото»: галерея по ракурсам, просмотр, сравнение, формы загрузки, правки и удаления, корзина.

Страницы только формируют HTML по соединению на чтение. Запись выполняет fitness.photo_store
(загрузка, правка) и fitness.photo_trash (корзина с резервной копией базы) — их вызывает app.py.
Скриптов на страницах нет: выбор для сравнения — обычные флажки, увеличение — отдельная страница.
"""

from datetime import date, timedelta
from html import escape

from .. import photo_store, photo_trash, records
from ..i18n import t
from . import pages

# Порядок секций галереи (коды ракурсов из базы). Фото без ракурса (старые записи) показываются в «Другое»
# с пометкой. Подписи — по ключам view.<код> в словарях fitness/locales.
SECTIONS = ["front", "back", "side", "other"]
SIZES = {"m": "60vh", "l": "85vh", "s": "40vh"}
NOTICES = ("uploaded", "saved", "deleted", "restored", "purged")   # код в адресе -> ключ photos.notice.<код>

CSS = """
main{max-width:1400px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0 4px}
.btn{display:inline-block;padding:6px 14px;border:1px solid var(--accent);border-radius:8px;background:var(--accent);
color:#fff;text-decoration:none;font:inherit;font-size:14px;cursor:pointer}
.btn.secondary{background:transparent;color:var(--accent)}.btn.danger{background:#b42318;border-color:#b42318}
.notice{border:1px solid var(--line);border-left:4px solid #12b76a;border-radius:8px;padding:8px 12px;margin-bottom:16px;
background:var(--card)}.notice.error{border-left-color:#b42318}
.sticky{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line);margin:0 -16px 12px;
padding:8px 16px}
.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;align-items:start}
.gallery section{border:1px solid var(--line);border-radius:10px;padding:10px;min-width:0}
.gallery h3{margin:0 0 8px;font-size:15px}
.shot{margin:0 0 14px;padding-bottom:12px;border-bottom:1px solid var(--line)}.shot:last-child{border-bottom:0}
.shot img{display:block;width:100%;height:auto;border-radius:8px;border:1px solid var(--line)}
.shot figcaption{font-size:13px;margin-top:4px}.shot label{display:block;font-size:13px;margin-top:4px;cursor:pointer}
.shot.marked img{outline:3px solid var(--accent);outline-offset:2px}
.big img{display:block;max-width:100%;max-height:85vh;height:auto;margin:0 auto;border-radius:8px}
.compare{display:flex;gap:16px;justify-content:center;align-items:flex-start}
.compare figure{margin:0;flex:0 1 auto;min-width:0;text-align:center}
.compare img{display:block;width:auto;max-width:100%;object-fit:contain;margin:0 auto;border-radius:8px;
border:1px solid var(--line)}
.compare figcaption{font-size:14px;margin-top:6px}
@media (max-width:700px){.compare{flex-direction:column;align-items:stretch}.compare img{width:100%;height:auto!important}}
form.grid{display:grid;grid-template-columns:max-content 1fr;gap:10px 14px;align-items:center;max-width:720px}
form.grid label{font-weight:600}form.grid .full{grid-column:1/-1}
@media (max-width:600px){form.grid{grid-template-columns:1fr}}
input[type=date],input[type=text],select,textarea{font:inherit;padding:5px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--text);max-width:100%}
textarea{width:100%;min-height:60px}
"""


def _page(title, body):
    return pages.layout(title, "/photos", f"<style>{CSS}</style>{body}")


def _notice(message, error=False):
    if not message:
        return ""
    cls = "notice error" if error else "notice"
    return f'<div class="{cls}">{escape(message)}</div>'


def _hidden(**fields):
    return "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(str(v))}">'
                   for k, v in fields.items() if v is not None)


def tabs(active, period, conn=None):
    """Вкладки «Галерея» и «Корзина» (с числом снимков в корзине)."""
    count = ""
    if conn is not None:
        count = f" ({conn.execute('SELECT COUNT(*) FROM photo_trash').fetchone()[0]})"
    items = (("gallery", "/photos", t("photos.tab.gallery")), ("trash", "/photos/trash", t("photos.tab.trash") + count))
    links = []
    for key, path, label in items:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a{cls} href="{pages.link(path, period=period)}">{escape(label)}</a>')
    return f'<div class="chips">{"".join(links)}</div>'


def view_label(view):
    return t("view." + view) if view in SECTIONS else t("view.unknown")


def _caption(row):
    parts = [escape(row["taken_on"])]
    if row["view"] not in SECTIONS:
        parts.append(f'<span class="muted">{t("view.unknown_short")}</span>')
    if row.get("weight_kg") is not None:
        parts.append(f"{pages.fmt(row['weight_kg'])} {pages.u('kg')}")
    notes = f'<br><span class="muted">{escape(row["notes"])}</span>' if row.get("notes") else ""
    return " · ".join(parts) + notes


def _photo_rows(conn, date_from, date_to):
    rows = conn.execute(
        "SELECT p.id, p.taken_on, p.file_path, p.view, p.notes, p.created_at, s.weight_kg FROM photos p"
        " LEFT JOIN measurement_sessions s ON s.id = p.session_id"
        " WHERE p.taken_on BETWEEN ? AND ? ORDER BY p.taken_on, p.created_at, p.id",
        (date_from, date_to),
    ).fetchall()
    return [dict(r) for r in rows]


def group_by_view(rows):
    """Фото по секциям, внутри — от старых к новым. Неизвестный или пустой ракурс — в «Другое»."""
    groups = {key: [] for key in SECTIONS}
    for row in rows:
        groups[row["view"] if row["view"] in groups else "other"].append(row)
    return groups


def gallery_page(conn, period, notice=None, marked=None):
    date_from, date_to = pages.period_bounds(conn, period)
    rows = _photo_rows(conn, date_from, date_to)
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    groups = group_by_view(rows)

    sections = []
    for key in SECTIONS:
        label = view_label(key)
        shots = []
        for r in groups[key]:
            checked = " checked" if marked == r["id"] else ""
            cls = "shot marked" if checked else "shot"
            shots.append(
                f'<figure class="{cls}"><a href="{pages.link("/photos/" + str(r["id"]), period=period)}" '
                f'title="{t("photos.open_large")}"><img src="/photo/{r["id"]}" '
                f'alt="{escape(label)}, {escape(r["taken_on"])}" loading="lazy"></a>'
                f'<figcaption>{_caption(r)}</figcaption>'
                f'<label><input type="checkbox" name="id" value="{r["id"]}"{checked}> {t("photos.for_compare")}'
                "</label></figure>")
        if not shots:
            shots.append(f'<p class="empty">{escape(t("photos.section_empty", view=label))}</p>'
                         f'<a class="btn secondary" href="{pages.link("/photos/upload", view=key, period=period)}">'
                         f'{t("photos.add_shot")}</a>')
        sections.append(f'<section><h3>{escape(label)} <span class="muted">({len(groups[key])})</span></h3>'
                        f'{"".join(shots)}</section>')

    upload = f'<a class="btn" href="{pages.link("/photos/upload", period=period)}">{t("photos.upload")}</a>'
    if total == 0:
        intro = f'<p class="empty">{escape(t("photos.none_yet"))}</p>'
    elif not rows:
        intro = f'<p class="empty">{escape(t("photos.none_in_period"))}</p>'
    else:
        intro = ""
    head = (f'<div class="card"><h2>{t("photos.title")}</h2>{tabs("gallery", period, conn)}'
            f'{pages.period_selector("/photos", period)}'
            f'<div class="toolbar">{upload}<span class="muted">{escape(t("photos.gallery_hint"))}</span></div>'
            f"{intro}</div>")
    gallery = (f'<form class="card" method="get" action="/photos/compare">{_hidden(period=period)}'
               f'<div class="sticky toolbar"><button class="btn" type="submit">{t("photos.compare")}</button>'
               f'<span class="muted">{escape(t("photos.gallery_period", start=date_from, end=date_to))}</span></div>'
               f'<div class="gallery">{"".join(sections)}</div></form>')
    return _page(t("nav.photos"), _notice(notice) + head + gallery)


def upload_page(period, csrf, error=None, values=None, today=None):
    values = values or {}
    today = today or date.today()
    taken_on = values.get("taken_on") or today.isoformat()
    max_date = (today + timedelta(days=1)).isoformat()
    first = values.get("view") if values.get("view") in SECTIONS else None
    inputs = []
    for key in sorted(SECTIONS, key=lambda s: s != first):
        inputs.append(f'<label for="f-{key}">{escape(view_label(key))}</label>'
                      f'<input id="f-{key}" type="file" name="file_{key}" multiple '
                      f'accept="image/jpeg,image/png,image/webp">')
    hint = t("photos.upload_hint", formats=photo_store.FORMAT_NAMES, mb=photo_store.MAX_FILE_BYTES // (1024 * 1024),
             n=photo_store.MAX_FILES)
    body = (
        _notice(error, error=True)
        + f'<div class="card"><h2>{t("photos.upload")}</h2><p class="muted">{escape(hint)}</p>'
        + (f'<p class="warn">{escape(t("photos.reselect_files"))}</p>' if error else "")
        + '<form class="grid" method="post" action="/photos/upload" enctype="multipart/form-data">'
        + _hidden(csrf=csrf, period=period)
        + f'<label for="taken_on">{t("photos.taken_on")}</label><input id="taken_on" type="date" name="taken_on" '
          f'value="{escape(taken_on)}" max="{max_date}" required>'
        + "".join(inputs)
        + f'<label for="notes">{t("photos.comment")}</label>'
          f'<input id="notes" type="text" name="notes" maxlength="2000" value="{escape(values.get("notes") or "")}" '
          f'placeholder="{t("photos.optional")}">'
        + f'<div class="full toolbar"><button class="btn" type="submit">{t("btn.save")}</button>'
          f'<a class="btn secondary" href="{pages.link("/photos", period=period)}">{t("btn.cancel")}</a></div>'
        + "</form></div>"
    )
    return _page(t("photos.upload"), body)


def _photo_or_none(conn, photo_id):
    row = conn.execute(
        "SELECT p.*, s.weight_kg FROM photos p LEFT JOIN measurement_sessions s ON s.id = p.session_id"
        " WHERE p.id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def missing_photo_page():
    return _page(t("photos.not_found"), f'<div class="card"><h2>{t("photos.not_found")}</h2>'
                                        f'<p><a href="/photos">{t("photos.to_gallery")}</a></p></div>')


def photo_page(conn, photo_id, period, csrf, notice=None, error=None, values=None):
    row = _photo_or_none(conn, photo_id)
    if row is None:
        return None
    values = values or {}
    view_now = values.get("view", row["view"] or "")
    options = [] if row["view"] in SECTIONS and view_now else [f'<option value="">{t("view.unset")}</option>']
    for key in SECTIONS:
        sel = " selected" if key == view_now else ""
        options.append(f'<option value="{key}"{sel}>{escape(view_label(key))}</option>')
    back = pages.link("/photos", period=period)
    compare = pages.link("/photos", period=period, mark=row["id"])
    body = (
        _notice(notice) + _notice(error, error=True)
        + f'<div class="card"><h2>{escape(view_label(row["view"]))} · {escape(row["taken_on"])}</h2>'
        f'<div class="toolbar"><a class="btn secondary" href="{back}">← {t("photos.to_gallery")}</a>'
        f'<a class="btn" href="{compare}">{t("photos.compare_with")}</a>'
        f'<a class="btn secondary" href="/photo/{row["id"]}">{t("photos.original")}</a></div>'
        f'<figure class="big"><img src="/photo/{row["id"]}" alt="{escape(view_label(row["view"]))}">'
        f'<figcaption>{_caption(row)}</figcaption></figure></div>'
        + f'<div class="card"><h2>{t("photos.edit_caption")}</h2>'
        f'<form class="grid" method="post" action="/photos/{row["id"]}/edit">'
        + _hidden(csrf=csrf, period=period, updated_at=row["updated_at"])
        + f'<label for="taken_on">{t("photos.taken_on")}</label><input id="taken_on" type="date" name="taken_on" '
          f'value="{escape(values.get("taken_on", row["taken_on"]))}" required>'
        + f'<label for="view">{t("photos.view")}</label><select id="view" name="view">{"".join(options)}</select>'
        + f'<label for="notes">{t("photos.comment")}</label><textarea id="notes" name="notes" maxlength="2000">'
          f'{escape(values.get("notes", row["notes"] or ""))}</textarea>'
        + f'<div class="full toolbar"><button class="btn" type="submit">{t("btn.save")}</button>'
          f'<a class="btn secondary" href="{back}">{t("btn.cancel")}</a>'
          f'<a class="btn secondary" href="{pages.link("/photos/" + str(row["id"]) + "/delete", period=period)}">'
          f'{t("photos.delete")}</a></div></form>'
        + f'<p class="muted">{escape(t("photos.file_note", path="data/photos/" + row["file_path"]))}</p></div>'
    )
    return _page(t("nav.photos"), body)


def delete_page(conn, photo_id, period, csrf, error=None):
    row = _photo_or_none(conn, photo_id)
    if row is None:
        return None
    code = records.prepare_delete(conn, "photo", row["id"])["confirmation_code"]
    body = (
        _notice(error, error=True)
        + f'<div class="card"><h2>{t("photos.delete_question")}</h2>'
        f'<figure class="big"><img src="/photo/{row["id"]}" alt="{t("photos.delete_alt")}" style="max-height:40vh">'
        f'<figcaption>{escape(view_label(row["view"]))} · {_caption(row)}</figcaption></figure>'
        f'<p>{escape(t("photos.delete_text"))}</p>'
        f'<form method="post" action="/photos/{row["id"]}/delete" class="toolbar">'
        + _hidden(csrf=csrf, period=period, code=code)
        + f'<button class="btn danger" type="submit">{t("photos.delete_confirm")}</button>'
          f'<a class="btn secondary" href="{pages.link("/photos/" + str(row["id"]), period=period)}">'
          f'{t("btn.cancel")}</a></form></div>'
    )
    return _page(t("photos.delete_title"), body)


def _trash_caption(item):
    parts = [f"<b>{escape(item['taken_on'])}</b>", escape(view_label(item["view"]))]
    notes = f'<br><span class="muted">{escape(item["notes"])}</span>' if item.get("notes") else ""
    deleted = (f'<br><span class="muted">{t("photos.deleted_at")} '
               f'{escape(item["deleted_at"].replace("T", " ").rstrip("Z"))} UTC</span>')
    return " · ".join(parts) + notes + deleted


def _trash_thumb(item, present, height=None):
    if not present:
        return f'<p class="warn">{escape(t("photos.trash_file_missing"))}</p>'
    style = f' style="max-height:{height}"' if height else ""
    return (f'<img src="/photo-trash/{item["id"]}" alt="{escape(view_label(item["view"]))}, '
            f'{escape(item["taken_on"])}" loading="lazy"{style}>')


def trash_page(conn, period, csrf, trash_dir, notice=None, error=None):
    rows = [dict(r) for r in conn.execute("SELECT * FROM photo_trash ORDER BY deleted_at DESC, id DESC")]
    shots = []
    for item in rows:
        present = photo_trash.trash_file(item, trash_dir) is not None
        purge = pages.link(f"/photos/trash/{item['id']}/purge", period=period)
        shots.append(
            f'<figure class="shot">{_trash_thumb(item, present)}<figcaption>{_trash_caption(item)}</figcaption>'
            f'<form method="post" action="/photos/trash/{item["id"]}/restore" class="toolbar">'
            + _hidden(csrf=csrf, period=period)
            + (f'<button class="btn" type="submit">{t("photos.restore")}</button>' if present else "")
            + f'<a class="btn secondary" href="{purge}">{t("photos.purge")}</a></form></figure>')
    if rows:
        content = f'<div class="gallery">{"".join(shots)}</div>'
    else:
        content = f'<p class="empty">{t("photos.trash_empty")}</p>'
    body = (
        _notice(notice) + _notice(error, error=True)
        + f'<div class="card"><h2>{t("photos.title")}</h2>{tabs("trash", period, conn)}'
        f'<p class="muted">{escape(t("photos.trash_hint"))}</p>'
        f"{content}</div>"
    )
    return _page(t("photos.trash_title"), body)


def purge_page(conn, trash_id, period, csrf, trash_dir, error=None):
    row = conn.execute("SELECT * FROM photo_trash WHERE id = ?", (trash_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    present = photo_trash.trash_file(item, trash_dir) is not None
    code = photo_trash.purge_code(item, present)
    what = t("photos.purge_what_file") if present else t("photos.purge_what_record")
    body = (
        _notice(error, error=True)
        + f'<div class="card"><h2>{t("photos.purge_question")}</h2>'
        f'<figure class="big">{_trash_thumb(item, present, "40vh")}'
        f"<figcaption>{_trash_caption(item)}</figcaption></figure>"
        f"<p>{escape(what)} {escape(t('photos.purge_text'))}</p>"
        f'<form method="post" action="/photos/trash/{item["id"]}/purge" class="toolbar">'
        + _hidden(csrf=csrf, period=period, code=code)
        + f'<button class="btn danger" type="submit">{t("photos.purge_confirm")}</button>'
          f'<a class="btn secondary" href="{pages.link("/photos/trash", period=period)}">{t("btn.cancel")}</a>'
          "</form></div>"
    )
    return _page(t("photos.purge_title"), body)


def missing_trash_page():
    return _page(t("not_found.title"), f'<div class="card"><h2>{t("photos.trash_not_found")}</h2>'
                                       f'<p><a href="/photos/trash">{t("photos.to_trash")}</a></p></div>')


def parse_compare_ids(values):
    """Ровно два разных номера фото, в порядке выбора, или None."""
    ids = []
    for value in values:
        if not value.isdigit() or len(value) > 9:
            return None
        if int(value) not in ids:
            ids.append(int(value))
    return ids if len(ids) == 2 else None


def compare_page(conn, raw_ids, period, size="m"):
    ids = parse_compare_ids(raw_ids)
    back = pages.link("/photos", period=period)
    back_button = f'<div class="card"><a class="btn" href="{back}">← {t("photos.to_gallery")}</a></div>'
    if ids is None:
        return _page(t("photos.compare_title"), _notice(t("photos.compare_need_two"), error=True) + back_button)
    photos = [_photo_or_none(conn, i) for i in ids]
    if None in photos:
        return _page(t("photos.compare_title"), _notice(t("photos.compare_missing"), error=True) + back_button)
    size = size if size in SIZES else "m"
    height = SIZES[size]
    a, b = photos
    warning = ""
    if a["view"] != b["view"]:
        warning = _notice(t("photos.compare_views_differ", a=view_label(a["view"]), b=view_label(b["view"])),
                          error=True)
    days = (date.fromisoformat(b["taken_on"]) - date.fromisoformat(a["taken_on"])).days
    gap = t("photos.compare_gap", n=abs(days)) if days else t("photos.compare_same_day")
    sizes = "".join(
        f'<a class="{"active" if key == size else ""}" '
        f'href="{pages.link("/photos/compare", period=period, size=key)}&amp;id={ids[0]}&amp;id={ids[1]}">'
        f'{t("photos.size." + key)}</a>' for key in ("s", "m", "l"))
    swap = f'{pages.link("/photos/compare", period=period, size=size)}&amp;id={ids[1]}&amp;id={ids[0]}'
    figures = "".join(
        f'<figure><a href="/photo/{p["id"]}" title="{t("photos.open_original")}"><img src="/photo/{p["id"]}" '
        f'style="height:{height}" alt="{escape(view_label(p["view"]))}, {escape(p["taken_on"])}"></a>'
        f'<figcaption><b>{escape(p["taken_on"])}</b> · {escape(view_label(p["view"]))}'
        + (f" · {pages.fmt(p['weight_kg'])} {pages.u('kg')}" if p.get("weight_kg") is not None else "")
        + (f'<br><span class="muted">{escape(p["notes"])}</span>' if p.get("notes") else "")
        + "</figcaption></figure>" for p in photos)
    body = (
        warning
        + f'<div class="card"><h2>{t("photos.compare_title")}</h2><div class="toolbar">'
        f'<a class="btn secondary" href="{back}">✕ {t("photos.compare_exit")}</a>'
        f'<a class="btn secondary" href="{swap}">⇄ {t("photos.compare_swap")}</a>'
        f'<span class="periods chips">{sizes}</span><span class="muted">{escape(gap)} '
        f'{escape(t("photos.compare_hint"))}</span></div>'
        f'<div class="compare">{figures}</div></div>'
    )
    return _page(t("photos.compare_title"), body)


def ok_notice(code, count=None):
    if code not in NOTICES:
        return None
    if code == "uploaded" and count and count.isdigit() and int(count) > 1:
        return t("photos.notice.uploaded_many", n=int(count))
    return t("photos.notice." + code)
