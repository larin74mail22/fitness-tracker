"""Алкоголь: записи и сводка."""

from . import validation as v
from .common import existing_by_request_id, round_or_none, row_to_dict
from .db import transaction
from .errors import NotFoundError, ValidationError

ETHANOL_DENSITY = 0.789      # г/мл
ETHANOL_KCAL_PER_G = 7.0
KCAL_SOURCES = ("user", "label")


def ethanol_grams(volume_ml, abv_percent):
    """Граммы чистого спирта; None, если объём или крепость неизвестны."""
    if volume_ml is None or abv_percent is None:
        return None
    return round(volume_ml * abv_percent / 100 * ETHANOL_DENSITY, 1)


def log_alcohol(conn, data):
    """Записать напиток.

    data: consumed_on (обяз.), drink (обяз.), consumed_time, volume_ml, abv_percent, kcal, kcal_source,
    notes, client_request_id.
    Если калории не указаны, но известны объём и крепость, считаются калории только от спирта
    (без сахара) и помечаются как оценка.
    """
    v.check_object(data, {"consumed_on", "consumed_time", "drink", "volume_ml", "abv_percent", "kcal",
                          "kcal_source", "notes", "client_request_id"}, "alcohol")
    consumed_on = v.parse_date(data.get("consumed_on"), "consumed_on")
    consumed_time = v.parse_time(data.get("consumed_time"), "consumed_time")
    drink = v.parse_text(data.get("drink"), "drink", required=True, max_len=v.MAX_SHORT_TEXT)
    volume = v.parse_number(data.get("volume_ml"), "volume_ml", min_value=0, min_exclusive=True, max_value=10000)
    abv = v.parse_number(data.get("abv_percent"), "abv_percent", min_value=0, min_exclusive=True, max_value=100)
    kcal = v.parse_number(data.get("kcal"), "kcal", min_value=0, max_value=10000)
    kcal_source = v.parse_enum(data.get("kcal_source"), "kcal_source", KCAL_SOURCES)
    notes = v.parse_text(data.get("notes"), "notes")
    request_id = v.parse_request_id(data.get("client_request_id"))

    if kcal_source is not None and kcal is None:
        raise ValidationError("kcal_source", "источник калорий указан без самих калорий (kcal)")
    if kcal is not None and kcal_source is None:
        raise ValidationError("kcal_source", "укажите источник калорий: user (со слов пользователя) или label")

    ethanol = ethanol_grams(volume, abv)
    if kcal is not None:
        kcal_is_estimate = kcal_source != "label"
    elif ethanol is not None:
        kcal = round(ethanol * ETHANOL_KCAL_PER_G, 1)
        kcal_source = "ethanol_only_estimate"
        kcal_is_estimate = True
    else:
        kcal_is_estimate = None

    with transaction(conn):
        existing = existing_by_request_id(conn, "alcohol_entries", request_id)
        if existing:
            return {"id": existing, "duplicate": True, "record": get_alcohol_entry(conn, existing)}
        entry_id = conn.execute(
            "INSERT INTO alcohol_entries (consumed_on, consumed_time, drink, volume_ml, abv_percent, ethanol_g,"
            " kcal, kcal_source, kcal_is_estimate, notes, client_request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (consumed_on, consumed_time, drink, volume, abv, ethanol, kcal, kcal_source,
             None if kcal_is_estimate is None else int(kcal_is_estimate), notes, request_id),
        ).lastrowid
    return {"id": entry_id, "duplicate": False, "record": get_alcohol_entry(conn, entry_id)}


def _entry_dict(row):
    record = row_to_dict(row)
    if record["kcal_is_estimate"] is not None:
        record["kcal_is_estimate"] = bool(record["kcal_is_estimate"])
    return record


def get_alcohol_entry(conn, entry_id):
    row = conn.execute("SELECT * FROM alcohol_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Запись об алкоголе с id={entry_id} не найдена")
    return _entry_dict(row)


def get_alcohol_summary(conn, date_from, date_to):
    start, end = v.parse_date_range(date_from, date_to)
    entries = [_entry_dict(r) for r in conn.execute(
        "SELECT * FROM alcohol_entries WHERE consumed_on BETWEEN ? AND ? ORDER BY consumed_on, consumed_time, id",
        (start, end),
    ).fetchall()]
    known_ethanol = [e["ethanol_g"] for e in entries if e["ethanol_g"] is not None]
    known_kcal = [e["kcal"] for e in entries if e["kcal"] is not None]
    return {
        "date_from": start, "date_to": end,
        "entries_count": len(entries),
        "days_with_alcohol": len({e["consumed_on"] for e in entries}),
        "ethanol_g_total": round_or_none(sum(known_ethanol)) if known_ethanol else None,
        "entries_without_ethanol": len(entries) - len(known_ethanol),
        "kcal_total": round_or_none(sum(known_kcal)) if known_kcal else None,
        "entries_without_kcal": len(entries) - len(known_kcal),
        "entries": entries,
        "note": ("Спирт считается только при известных объёме и крепости. "
                 "kcal_source=ethanol_only_estimate — калории только от спирта, без сахара."),
    }
