"""Раздел «Фото»: галерея по ракурсам, просмотр, сравнение, формы загрузки, правки и удаления, корзина.

Страницы только формируют HTML по соединению на чтение. Запись выполняет fitness.photo_store
(загрузка, правка) и fitness.photo_trash (корзина с резервной копией базы) — их вызывает app.py.
Скриптов на страницах нет: выбор для сравнения — обычные флажки, увеличение — отдельная страница.
"""

from datetime import date, timedelta
from html import escape

from .. import photo_store, photo_trash, records
from . import pages

# Порядок секций галереи. Фото без ракурса (старые записи) показываются в «Другое» с пометкой.
SECTIONS = [("front", "Спереди"), ("back", "Сзади"), ("side", "Сбоку"), ("other", "Другое")]
VIEW_LABELS = dict(SECTIONS)
SIZES = {"m": "60vh", "l": "85vh", "s": "40vh"}
NOTICES = {
    "uploaded": "Фото сохранено.",
    "saved": "Изменения сохранены.",
    "deleted": "Фото перенесено в корзину. Резервная копия базы сделана; восстановить можно на вкладке «Корзина».",
    "restored": "Фото восстановлено из корзины и снова есть в галерее.",
    "purged": "Фото удалено навсегда. Резервная копия базы сделана.",
}

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
    items = (("gallery", "/photos", "Галерея"), ("trash", "/photos/trash", "Корзина" + count))
    links = []
    for key, path, label in items:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a{cls} href="{pages.link(path, period=period)}">{escape(label)}</a>')
    return f'<div class="chips">{"".join(links)}</div>'


def view_label(view):
    return VIEW_LABELS.get(view, "Ракурс не указан")


def _caption(row):
    parts = [escape(row["taken_on"])]
    if row["view"] not in VIEW_LABELS:
        parts.append('<span class="muted">ракурс не указан</span>')
    if row.get("weight_kg") is not None:
        parts.append(f"{pages.fmt(row['weight_kg'])} кг")
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
    groups = {key: [] for key, _ in SECTIONS}
    for row in rows:
        groups[row["view"] if row["view"] in groups else "other"].append(row)
    return groups


def gallery_page(conn, period, notice=None, marked=None):
    date_from, date_to = pages.period_bounds(conn, period)
    rows = _photo_rows(conn, date_from, date_to)
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    groups = group_by_view(rows)

    sections = []
    for key, label in SECTIONS:
        shots = []
        for r in groups[key]:
            checked = " checked" if marked == r["id"] else ""
            cls = "shot marked" if checked else "shot"
            shots.append(
                f'<figure class="{cls}"><a href="{pages.link("/photos/" + str(r["id"]), period=period)}" '
                f'title="Открыть крупно"><img src="/photo/{r["id"]}" alt="{escape(label)}, {escape(r["taken_on"])}" '
                f'loading="lazy"></a><figcaption>{_caption(r)}</figcaption>'
                f'<label><input type="checkbox" name="id" value="{r["id"]}"{checked}> для сравнения</label></figure>')
        if not shots:
            shots.append(f'<p class="empty">Снимков «{escape(label)}» за этот период нет.</p>'
                         f'<a class="btn secondary" href="{pages.link("/photos/upload", view=key, period=period)}">'
                         "Добавить снимок</a>")
        sections.append(f'<section><h3>{escape(label)} <span class="muted">({len(groups[key])})</span></h3>'
                        f'{"".join(shots)}</section>')

    upload = f'<a class="btn" href="{pages.link("/photos/upload", period=period)}">Загрузить фото</a>'
    if total == 0:
        intro = ('<p class="empty">Фото за этот период нет. Нажмите «Загрузить фото», выберите дату и ракурс — '
                 "снимки появятся здесь в колонках по ракурсам.</p>")
    elif not rows:
        intro = '<p class="empty">Фото за этот период нет. Выберите период подлиннее.</p>'
    else:
        intro = ""
    head = (f'<div class="card"><h2>Фото прогресса</h2>{tabs("gallery", period, conn)}'
            f'{pages.period_selector("/photos", period)}'
            f'<div class="toolbar">{upload}<span class="muted">Отметьте два снимка и нажмите «Сравнить». '
            "Нажатие на снимок открывает его крупно.</span></div>"
            f"{intro}</div>")
    gallery = (f'<form class="card" method="get" action="/photos/compare">{_hidden(period=period)}'
               f'<div class="sticky toolbar"><button class="btn" type="submit">Сравнить</button>'
               f'<span class="muted">Период: {escape(date_from)} — {escape(date_to)}. '
               f"Внутри колонки снимки идут от старых к новым.</span></div>"
               f'<div class="gallery">{"".join(sections)}</div></form>')
    return _page("Фото", _notice(notice) + head + gallery)


def upload_page(period, csrf, error=None, values=None, today=None):
    values = values or {}
    today = today or date.today()
    taken_on = values.get("taken_on") or today.isoformat()
    max_date = (today + timedelta(days=1)).isoformat()
    first = values.get("view") if values.get("view") in VIEW_LABELS else None
    inputs = []
    for key, label in sorted(SECTIONS, key=lambda s: s[0] != first):
        inputs.append(f'<label for="f-{key}">{escape(label)}</label>'
                      f'<input id="f-{key}" type="file" name="file_{key}" multiple '
                      f'accept="image/jpeg,image/png,image/webp">')
    body = (
        _notice(error, error=True)
        + '<div class="card"><h2>Загрузить фото</h2>'
        '<p class="muted">Выберите файлы в поле нужного ракурса: можно сразу несколько ракурсов и несколько файлов '
        f"в каждом. Форматы: {photo_store.FORMAT_NAMES}, до {photo_store.MAX_FILE_BYTES // (1024 * 1024)} МБ "
        f"на файл, до {photo_store.MAX_FILES} файлов за раз. Переименовывать файлы не нужно.</p>"
        + ('<p class="warn">Файлы нужно выбрать заново: браузер не сохраняет их после ошибки.</p>' if error else "")
        + '<form class="grid" method="post" action="/photos/upload" enctype="multipart/form-data">'
        + _hidden(csrf=csrf, period=period)
        + f'<label for="taken_on">Дата съёмки</label><input id="taken_on" type="date" name="taken_on" '
          f'value="{escape(taken_on)}" max="{max_date}" required>'
        + "".join(inputs)
        + '<label for="notes">Комментарий</label>'
          f'<input id="notes" type="text" name="notes" maxlength="2000" value="{escape(values.get("notes") or "")}" '
          'placeholder="необязательно">'
        + '<div class="full toolbar"><button class="btn" type="submit">Сохранить</button>'
          f'<a class="btn secondary" href="{pages.link("/photos", period=period)}">Отмена</a></div>'
        + "</form></div>"
    )
    return _page("Загрузить фото", body)


def _photo_or_none(conn, photo_id):
    row = conn.execute(
        "SELECT p.*, s.weight_kg FROM photos p LEFT JOIN measurement_sessions s ON s.id = p.session_id"
        " WHERE p.id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def missing_photo_page():
    return _page("Фото не найдено", '<div class="card"><h2>Фото не найдено</h2>'
                                    '<p><a href="/photos">К галерее</a></p></div>')


def photo_page(conn, photo_id, period, csrf, notice=None, error=None, values=None):
    row = _photo_or_none(conn, photo_id)
    if row is None:
        return None
    values = values or {}
    view_now = values.get("view", row["view"] or "")
    options = [] if row["view"] in VIEW_LABELS and view_now else ['<option value="">не указан</option>']
    for key, label in SECTIONS:
        sel = " selected" if key == view_now else ""
        options.append(f'<option value="{key}"{sel}>{escape(label)}</option>')
    back = pages.link("/photos", period=period)
    compare = pages.link("/photos", period=period, mark=row["id"])
    body = (
        _notice(notice) + _notice(error, error=True)
        + f'<div class="card"><h2>{escape(view_label(row["view"]))} · {escape(row["taken_on"])}</h2>'
        f'<div class="toolbar"><a class="btn secondary" href="{back}">← К галерее</a>'
        f'<a class="btn" href="{compare}">Сравнить с другим фото</a>'
        f'<a class="btn secondary" href="/photo/{row["id"]}">Оригинал в полном размере</a></div>'
        f'<figure class="big"><img src="/photo/{row["id"]}" alt="{escape(view_label(row["view"]))}">'
        f'<figcaption>{_caption(row)}</figcaption></figure></div>'
        + '<div class="card"><h2>Изменить подпись</h2>'
        f'<form class="grid" method="post" action="/photos/{row["id"]}/edit">'
        + _hidden(csrf=csrf, period=period, updated_at=row["updated_at"])
        + f'<label for="taken_on">Дата съёмки</label><input id="taken_on" type="date" name="taken_on" '
          f'value="{escape(values.get("taken_on", row["taken_on"]))}" required>'
        + f'<label for="view">Ракурс</label><select id="view" name="view">{"".join(options)}</select>'
        + '<label for="notes">Комментарий</label><textarea id="notes" name="notes" maxlength="2000">'
          f'{escape(values.get("notes", row["notes"] or ""))}</textarea>'
        + '<div class="full toolbar"><button class="btn" type="submit">Сохранить</button>'
          f'<a class="btn secondary" href="{back}">Отмена</a>'
          f'<a class="btn secondary" href="{pages.link("/photos/" + str(row["id"]) + "/delete", period=period)}">'
          "Удалить…</a></div></form>"
        + f'<p class="muted">Файл: data/photos/{escape(row["file_path"])}. Правка подписи файл не меняет.</p></div>'
    )
    return _page("Фото", body)


def delete_page(conn, photo_id, period, csrf, error=None):
    row = _photo_or_none(conn, photo_id)
    if row is None:
        return None
    code = records.prepare_delete(conn, "photo", row["id"])["confirmation_code"]
    body = (
        _notice(error, error=True)
        + '<div class="card"><h2>Удалить фото?</h2>'
        f'<figure class="big"><img src="/photo/{row["id"]}" alt="удаляемое фото" style="max-height:40vh">'
        f'<figcaption>{escape(view_label(row["view"]))} · {_caption(row)}</figcaption></figure>'
        "<p>Фото исчезнет из галереи и из сравнения и попадёт в корзину: файл переносится из data/photos "
        "в data/photos_trash, дата, ракурс и комментарий сохраняются. Перед удалением автоматически делается "
        "и проверяется резервная копия базы (папка data/backups). Вернуть фото можно на вкладке «Корзина».</p>"
        f'<form method="post" action="/photos/{row["id"]}/delete" class="toolbar">'
        + _hidden(csrf=csrf, period=period, code=code)
        + '<button class="btn danger" type="submit">Да, переместить в корзину</button>'
          f'<a class="btn secondary" href="{pages.link("/photos/" + str(row["id"]), period=period)}">Отмена</a>'
          "</form></div>"
    )
    return _page("Удалить фото", body)


def _trash_caption(item):
    parts = [f"<b>{escape(item['taken_on'])}</b>", escape(view_label(item["view"]))]
    notes = f'<br><span class="muted">{escape(item["notes"])}</span>' if item.get("notes") else ""
    deleted = f'<br><span class="muted">Удалено: {escape(item["deleted_at"].replace("T", " ").rstrip("Z"))} UTC</span>'
    return " · ".join(parts) + notes + deleted


def _trash_thumb(item, present, height=None):
    if not present:
        return '<p class="warn">Файл в корзине не найден (data/photos_trash).</p>'
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
            + ('<button class="btn" type="submit">Восстановить</button>' if present else "")
            + f'<a class="btn secondary" href="{purge}">Удалить навсегда…</a></form></figure>')
    if rows:
        content = f'<div class="gallery">{"".join(shots)}</div>'
    else:
        content = '<p class="empty">Корзина пуста.</p>'
    body = (
        _notice(notice) + _notice(error, error=True)
        + f'<div class="card"><h2>Фото прогресса</h2>{tabs("trash", period, conn)}'
        '<p class="muted">Удалённые снимки не показываются в галерее и в сравнении. «Восстановить» возвращает '
        "снимок с прежними датой, ракурсом и комментарием. «Удалить навсегда» удаляет файл из data/photos_trash "
        "и запись корзины после отдельного подтверждения и резервной копии базы.</p>"
        f"{content}</div>"
    )
    return _page("Корзина фото", body)


def purge_page(conn, trash_id, period, csrf, trash_dir, error=None):
    row = conn.execute("SELECT * FROM photo_trash WHERE id = ?", (trash_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    present = photo_trash.trash_file(item, trash_dir) is not None
    code = photo_trash.purge_code(item, present)
    what = ("Файл будет удалён из папки data/photos_trash, а запись — из корзины." if present else
            "Файла в корзине уже нет: будет удалена только запись корзины.")
    body = (
        _notice(error, error=True)
        + '<div class="card"><h2>Удалить фото навсегда?</h2>'
        f'<figure class="big">{_trash_thumb(item, present, "40vh")}'
        f"<figcaption>{_trash_caption(item)}</figcaption></figure>"
        f"<p>{what} Восстановить фото после этого будет нельзя. Перед удалением автоматически делается "
        "и проверяется резервная копия базы (папка data/backups).</p>"
        f'<form method="post" action="/photos/trash/{item["id"]}/purge" class="toolbar">'
        + _hidden(csrf=csrf, period=period, code=code)
        + '<button class="btn danger" type="submit">Да, удалить навсегда</button>'
          f'<a class="btn secondary" href="{pages.link("/photos/trash", period=period)}">Отмена</a>'
          "</form></div>"
    )
    return _page("Удалить навсегда", body)


def missing_trash_page():
    return _page("Не найдено", '<div class="card"><h2>Фото в корзине не найдено</h2>'
                               '<p><a href="/photos/trash">К корзине</a></p></div>')


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
    if ids is None:
        return _page("Сравнение", _notice("Для сравнения отметьте ровно две фотографии.", error=True)
                     + f'<div class="card"><a class="btn" href="{back}">← К галерее</a></div>')
    photos = [_photo_or_none(conn, i) for i in ids]
    if None in photos:
        return _page("Сравнение", _notice("Одна из выбранных фотографий не найдена.", error=True)
                     + f'<div class="card"><a class="btn" href="{back}">← К галерее</a></div>')
    size = size if size in SIZES else "m"
    height = SIZES[size]
    a, b = photos
    warning = ""
    if a["view"] != b["view"]:
        warning = _notice(f"Ракурсы разные: «{view_label(a['view'])}» и «{view_label(b['view'])}». "
                          "Сравнение может вводить в заблуждение.", error=True)
    days = (date.fromisoformat(b["taken_on"]) - date.fromisoformat(a["taken_on"])).days
    gap = f"Между снимками {abs(days)} дн." if days else "Снимки одной даты."
    sizes = "".join(
        f'<a class="{"active" if key == size else ""}" '
        f'href="{pages.link("/photos/compare", period=period, size=key)}&amp;id={ids[0]}&amp;id={ids[1]}">'
        f"{label}</a>" for key, label in (("s", "Мелко"), ("m", "Средне"), ("l", "Крупно")))
    swap = f'{pages.link("/photos/compare", period=period, size=size)}&amp;id={ids[1]}&amp;id={ids[0]}'
    figures = "".join(
        f'<figure><a href="/photo/{p["id"]}" title="Открыть оригинал"><img src="/photo/{p["id"]}" '
        f'style="height:{height}" alt="{escape(view_label(p["view"]))}, {escape(p["taken_on"])}"></a>'
        f'<figcaption><b>{escape(p["taken_on"])}</b> · {escape(view_label(p["view"]))}'
        + (f" · {pages.fmt(p['weight_kg'])} кг" if p.get("weight_kg") is not None else "")
        + (f'<br><span class="muted">{escape(p["notes"])}</span>' if p.get("notes") else "")
        + "</figcaption></figure>" for p in photos)
    body = (
        warning
        + f'<div class="card"><h2>Сравнение</h2><div class="toolbar">'
        f'<a class="btn secondary" href="{back}">✕ Выйти из сравнения</a>'
        f'<a class="btn secondary" href="{swap}">⇄ Поменять местами</a>'
        f'<span class="periods chips">{sizes}</span><span class="muted">{escape(gap)} '
        "Нажатие на снимок открывает оригинал для увеличения.</span></div>"
        f'<div class="compare">{figures}</div></div>'
    )
    return _page("Сравнение", body)


def ok_notice(code, count=None):
    message = NOTICES.get(code)
    if code == "uploaded" and count and count.isdigit() and int(count) > 1:
        message = f"Сохранено фото: {int(count)}."
    return message

