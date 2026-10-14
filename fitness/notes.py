"""Общие заметки с дополнительными структурированными полями."""

from . import validation as v
from .common import existing_by_request_id, row_to_dict
from .db import transaction
from .errors import NotFoundError, ValidationError


def _parse_field(raw, where):
    v.check_object(raw, {"key", "value", "unit"}, where)
    key = v.parse_text(raw.get("key"), f"{where}.key", required=True, max_len=50)
    value = raw.get("value")
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{where}.value", "значение обязательно: число или текст")
    if isinstance(value, (int, float)):
        value_num, value_text = v.parse_number(value, f"{where}.value"), None
    else:
        value_num, value_text = None, v.parse_text(value, f"{where}.value", required=True, max_len=v.MAX_SHORT_TEXT)
    unit = v.parse_text(raw.get("unit"), f"{where}.unit", max_len=20)
    return {"key": key, "value_num": value_num, "value_text": value_text, "unit": unit}


def add_note(conn, data):
    """data: note_date (обяз.), category (обяз.), text (обяз.), fields=[{key, value, unit}], client_request_id."""
    v.check_object(data, {"note_date", "category", "text", "fields", "client_request_id"}, "note")
    note_date = v.parse_date(data.get("note_date"), "note_date")
    category = v.parse_text(data.get("category"), "category", required=True, max_len=50).lower()
    text = v.parse_text(data.get("text"), "text", required=True)
    request_id = v.parse_request_id(data.get("client_request_id"))
    raw_fields = v.parse_list(data.get("fields"), "fields", max_items=30)
    fields = [_parse_field(f, f"fields[{i}]") for i, f in enumerate(raw_fields)]
    keys = [v.normalize_key(f["key"]) for f in fields]
    if len(set(keys)) != len(keys):
        raise ValidationError("fields", "одинаковые ключи в дополнительных полях")

    with transaction(conn):
        existing = existing_by_request_id(conn, "notes", request_id)
        if existing:
            return {"id": existing, "duplicate": True, "record": get_note(conn, existing)}
        note_id = conn.execute(
            "INSERT INTO notes (note_date, category, text, client_request_id) VALUES (?, ?, ?, ?)",
            (note_date, category, text, request_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO note_fields (note_id, key, value_text, value_num, unit) VALUES (?, ?, ?, ?, ?)",
            [(note_id, f["key"], f["value_text"], f["value_num"], f["unit"]) for f in fields],
        )
    return {"id": note_id, "duplicate": False, "record": get_note(conn, note_id)}


def get_note(conn, note_id):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Заметка с id={note_id} не найдена")
    record = row_to_dict(row)
    record["fields"] = [
        {"key": r["key"], "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
         "unit": r["unit"]}
        for r in conn.execute("SELECT * FROM note_fields WHERE note_id = ? ORDER BY id", (note_id,))
    ]
    return record
