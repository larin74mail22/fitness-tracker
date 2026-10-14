"""Вес, замеры тела и фотографии."""

from pathlib import Path, PureWindowsPath

from . import validation as v
from .common import existing_by_request_id, row_to_dict
from .db import PHOTOS_DIR, transaction
from .errors import NotFoundError, ValidationError

PHOTO_VIEWS = ("front", "side", "back", "other")
PHOTO_VIEW_ALIASES = {"спереди": "front", "сбоку": "side", "сзади": "back", "другое": "other"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def measurement_types(conn):
    rows = conn.execute("SELECT code, name_ru, unit, min_value, max_value FROM measurement_types ORDER BY code")
    return {r["code"]: dict(r) for r in rows}


def log_measurement(conn, data):
    """Записать вес и/или замеры за одну дату.

    data: measured_on (обяз.), measured_time, weight_kg, measurements=[{type, value}], notes, client_request_id.
    """
    v.check_object(data, {"measured_on", "measured_time", "weight_kg", "measurements", "notes",
                          "client_request_id"}, "measurement")
    measured_on = v.parse_date(data.get("measured_on"), "measured_on")
    measured_time = v.parse_time(data.get("measured_time"), "measured_time")
    weight = v.parse_number(data.get("weight_kg"), "weight_kg", min_value=20, max_value=400)
    notes = v.parse_text(data.get("notes"), "notes")
    request_id = v.parse_request_id(data.get("client_request_id"))
    raw_items = v.parse_list(data.get("measurements"), "measurements", max_items=30)
    if weight is None and not raw_items:
        raise ValidationError("measurement", "нужно указать вес (weight_kg) или хотя бы один замер (measurements)")

    types = measurement_types(conn)
    items, seen = [], set()
    for i, raw in enumerate(raw_items):
        where = f"measurements[{i}]"
        v.check_object(raw, {"type", "value"}, where)
        code = v.parse_enum(raw.get("type"), f"{where}.type", tuple(types), required=True)
        if code in seen:
            raise ValidationError(f"{where}.type", f"замер {code} указан дважды")
        seen.add(code)
        t = types[code]
        value = v.parse_number(raw.get("value"), f"{where}.value", required=True,
                               min_value=t["min_value"], max_value=t["max_value"])
        items.append((code, value))

    with transaction(conn):
        existing = existing_by_request_id(conn, "measurement_sessions", request_id)
        if existing:
            return {"id": existing, "duplicate": True, "record": get_measurement(conn, existing)}
        cur = conn.execute(
            "INSERT INTO measurement_sessions (measured_on, measured_time, weight_kg, notes, client_request_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (measured_on, measured_time, weight, notes, request_id),
        )
        session_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO body_measurements (session_id, type_code, value) VALUES (?, ?, ?)",
            [(session_id, code, value) for code, value in items],
        )
    return {"id": session_id, "duplicate": False, "record": get_measurement(conn, session_id)}


def _session_dict(conn, row):
    record = row_to_dict(row)
    record["measurements"] = [
        dict(r) for r in conn.execute(
            "SELECT b.type_code AS type, t.name_ru, b.value, t.unit"
            " FROM body_measurements b JOIN measurement_types t ON t.code = b.type_code"
            " WHERE b.session_id = ? ORDER BY b.type_code",
            (row["id"],),
        )
    ]
    record["photos"] = [
        dict(r) for r in conn.execute(
            "SELECT id, file_path, view, notes FROM photos WHERE session_id = ? ORDER BY id", (row["id"],)
        )
    ]
    return record


def get_measurement(conn, session_id):
    row = conn.execute("SELECT * FROM measurement_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Замер с id={session_id} не найден")
    return _session_dict(conn, row)


def get_measurements(conn, date_from, date_to, measurement_type=None):
    start, end = v.parse_date_range(date_from, date_to)
    sql = "SELECT * FROM measurement_sessions WHERE measured_on BETWEEN ? AND ?"
    params = [start, end]
    if measurement_type is not None:
        if measurement_type == "weight":
            sql += " AND weight_kg IS NOT NULL"
        else:
            code = v.parse_enum(measurement_type, "measurement_type", tuple(measurement_types(conn)) + ("weight",))
            sql += " AND id IN (SELECT session_id FROM body_measurements WHERE type_code = ?)"
            params.append(code)
    sql += " ORDER BY measured_on, measured_time, id"
    records = [_session_dict(conn, row) for row in conn.execute(sql, params).fetchall()]
    return {"date_from": start, "date_to": end, "count": len(records), "records": records}


def _clean_photo_path(value, photos_dir):
    rel = v.parse_text(value, "file_path", required=True, max_len=260)
    candidate = PureWindowsPath(rel)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise ValidationError("file_path", "нужен путь внутри папки data/photos, без '..' и без диска")
    if candidate.suffix.lower() not in PHOTO_EXTENSIONS:
        raise ValidationError("file_path", f"неподдерживаемый тип файла; допустимо: {', '.join(sorted(PHOTO_EXTENSIONS))}")
    full = (Path(photos_dir) / Path(*candidate.parts)).resolve()
    if Path(photos_dir).resolve() not in full.parents:
        raise ValidationError("file_path", "файл должен лежать внутри папки data/photos")
    if not full.is_file():
        raise ValidationError("file_path", f"файл не найден в data/photos: {rel}")
    return "/".join(candidate.parts)


def add_photo(conn, data, photos_dir=PHOTOS_DIR):
    """Привязать уже лежащий в data/photos файл к дате (и, если указано, к замеру)."""
    v.check_object(data, {"taken_on", "file_path", "session_id", "view", "notes"}, "photo")
    taken_on = v.parse_date(data.get("taken_on"), "taken_on")
    file_path = _clean_photo_path(data.get("file_path"), photos_dir)
    session_id = v.parse_number(data.get("session_id"), "session_id", integer=True, min_value=1)
    view = v.parse_enum(data.get("view"), "view", PHOTO_VIEWS, aliases=PHOTO_VIEW_ALIASES)
    notes = v.parse_text(data.get("notes"), "notes")

    with transaction(conn):
        if session_id is not None:
            get_measurement(conn, session_id)
        existing = conn.execute("SELECT id FROM photos WHERE file_path = ?", (file_path,)).fetchone()
        if existing:
            raise ValidationError("file_path", f"это фото уже записано (id={existing[0]})")
        cur = conn.execute(
            "INSERT INTO photos (session_id, taken_on, file_path, view, notes) VALUES (?, ?, ?, ?, ?)",
            (session_id, taken_on, file_path, view, notes),
        )
    row = conn.execute("SELECT * FROM photos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"id": cur.lastrowid, "record": dict(row)}
