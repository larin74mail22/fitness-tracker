"""Питание: приёмы пищи, продукты, сводка КБЖУ."""

from . import validation as v
from .common import existing_by_request_id, round_or_none, row_to_dict
from .db import transaction
from .errors import NotFoundError, ValidationError

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack", "other")
MEAL_TYPE_ALIASES = {"завтрак": "breakfast", "обед": "lunch", "ужин": "dinner", "перекус": "snack",
                     "другое": "other"}
UNITS = ("g", "ml", "pcs", "serving")
UNIT_ALIASES = {"г": "g", "гр": "g", "грамм": "g", "мл": "ml", "шт": "pcs", "штука": "pcs",
                "штуки": "pcs", "порция": "serving", "порции": "serving"}
SOURCES = ("label", "database", "recipe", "user", "estimate")
SOURCE_ALIASES = {"этикетка": "label", "упаковка": "label", "база": "database", "рецепт": "recipe",
                  "пользователь": "user", "оценка": "estimate"}
NUTRIENTS = ("kcal", "protein_g", "fat_g", "carbs_g")
NUTRIENT_MAX = {"kcal": 10000, "protein_g": 1000, "fat_g": 1000, "carbs_g": 2000}


def _parse_item(raw, where):
    v.check_object(raw, {"name", "quantity", "unit", "nutrition_source", "is_estimate", *NUTRIENTS}, where)
    item = {
        "name": v.parse_text(raw.get("name"), f"{where}.name", required=True, max_len=v.MAX_SHORT_TEXT),
        "quantity": v.parse_number(raw.get("quantity"), f"{where}.quantity", min_value=0, min_exclusive=True,
                                   max_value=100000),
        "unit": v.parse_enum(raw.get("unit"), f"{where}.unit", UNITS, aliases=UNIT_ALIASES),
    }
    if (item["quantity"] is None) != (item["unit"] is None):
        raise ValidationError(where, "количество (quantity) и единица (unit) указываются только вместе")
    for key in NUTRIENTS:
        item[key] = v.parse_number(raw.get(key), f"{where}.{key}", min_value=0, max_value=NUTRIENT_MAX[key])

    has_values = any(item[k] is not None for k in NUTRIENTS)
    source = v.parse_enum(raw.get("nutrition_source"), f"{where}.nutrition_source", SOURCES,
                          aliases=SOURCE_ALIASES)
    if has_values and source is None:
        raise ValidationError(f"{where}.nutrition_source",
                              "укажите, откуда взяты КБЖУ: label, database, recipe, user или estimate")
    # По умолчанию данные считаются приблизительными; точными — только если об этом сказано явно.
    is_estimate = v.parse_bool(raw.get("is_estimate"), f"{where}.is_estimate", default=True)
    if source == "estimate" and not is_estimate:
        raise ValidationError(f"{where}.is_estimate", "оценка (estimate) не может быть отмечена как точная")
    item["nutrition_source"] = source
    item["is_estimate"] = is_estimate
    return item


def log_meal(conn, data):
    """Записать приём пищи из одного или нескольких продуктов.

    data: eaten_on (обяз.), items (обяз., ≥1), eaten_time, meal_type, notes, client_request_id.
    """
    v.check_object(data, {"eaten_on", "eaten_time", "meal_type", "notes", "items", "client_request_id"}, "meal")
    eaten_on = v.parse_date(data.get("eaten_on"), "eaten_on")
    eaten_time = v.parse_time(data.get("eaten_time"), "eaten_time")
    meal_type = v.parse_enum(data.get("meal_type"), "meal_type", MEAL_TYPES, aliases=MEAL_TYPE_ALIASES)
    notes = v.parse_text(data.get("notes"), "notes")
    request_id = v.parse_request_id(data.get("client_request_id"))
    raw_items = v.parse_list(data.get("items"), "items", required=True, min_items=1, max_items=50)
    items = [_parse_item(it, f"items[{i}]") for i, it in enumerate(raw_items)]

    with transaction(conn):
        existing = existing_by_request_id(conn, "meals", request_id)
        if existing:
            return {"id": existing, "duplicate": True, "record": get_meal(conn, existing)}
        meal_id = conn.execute(
            "INSERT INTO meals (eaten_on, eaten_time, meal_type, notes, client_request_id) VALUES (?, ?, ?, ?, ?)",
            (eaten_on, eaten_time, meal_type, notes, request_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO meal_items (meal_id, position, name, quantity, unit, kcal, protein_g, fat_g, carbs_g,"
            " nutrition_source, is_estimate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(meal_id, pos, it["name"], it["quantity"], it["unit"], it["kcal"], it["protein_g"], it["fat_g"],
              it["carbs_g"], it["nutrition_source"], int(it["is_estimate"]))
             for pos, it in enumerate(items, start=1)],
        )
    return {"id": meal_id, "duplicate": False, "record": get_meal(conn, meal_id)}


def _meal_dict(conn, row):
    record = row_to_dict(row)
    items = []
    for r in conn.execute(
        "SELECT position, name, quantity, unit, kcal, protein_g, fat_g, carbs_g, nutrition_source, is_estimate"
        " FROM meal_items WHERE meal_id = ? ORDER BY position",
        (row["id"],),
    ):
        item = dict(r)
        item["is_estimate"] = bool(item["is_estimate"])
        items.append(item)
    record["items"] = items
    return record


def get_meal(conn, meal_id):
    row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Приём пищи с id={meal_id} не найден")
    return _meal_dict(conn, row)


def get_meals(conn, date_from, date_to):
    start, end = v.parse_date_range(date_from, date_to)
    rows = conn.execute(
        "SELECT * FROM meals WHERE eaten_on BETWEEN ? AND ? ORDER BY eaten_on, eaten_time, id", (start, end)
    ).fetchall()
    records = [_meal_dict(conn, row) for row in rows]
    return {"date_from": start, "date_to": end, "count": len(records), "records": records}


def get_nutrition_summary(conn, date_from, date_to, include_meals=False):
    """Итоги КБЖУ по дням. Суммы — по известным значениям; сколько значений неизвестно — указано отдельно."""
    start, end = v.parse_date_range(date_from, date_to)
    rows = conn.execute(
        "SELECT m.eaten_on AS day,"
        " COUNT(DISTINCT m.id) AS meals, COUNT(i.id) AS items,"
        " SUM(i.kcal) AS kcal, SUM(i.protein_g) AS protein_g, SUM(i.fat_g) AS fat_g, SUM(i.carbs_g) AS carbs_g,"
        " SUM(i.kcal IS NULL) AS items_without_kcal,"
        " SUM(i.is_estimate) AS items_estimated"
        " FROM meals m JOIN meal_items i ON i.meal_id = m.id"
        " WHERE m.eaten_on BETWEEN ? AND ? GROUP BY m.eaten_on ORDER BY m.eaten_on",
        (start, end),
    ).fetchall()
    days = []
    for r in rows:
        day = {"date": r["day"], "meals": r["meals"], "items": r["items"],
               "items_without_kcal": r["items_without_kcal"], "items_estimated": r["items_estimated"],
               "totals_complete": r["items_without_kcal"] == 0}
        for key in NUTRIENTS:
            day[key] = round_or_none(r[key])
        days.append(day)

    total = {key: round_or_none(sum(d[key] for d in days if d[key] is not None)) if days else None
             for key in NUTRIENTS}
    logged_days = [d for d in days if d["kcal"] is not None]
    result = {
        "date_from": start, "date_to": end,
        "days_with_records": len(days),
        "totals": total,
        "average_kcal_per_logged_day": (round(sum(d["kcal"] for d in logged_days) / len(logged_days), 1)
                                        if logged_days else None),
        "days": days,
        "note": "Суммы только по известным значениям. Если totals_complete=false, часть продуктов без калорий.",
    }
    if include_meals:
        result["meals"] = get_meals(conn, start, end)["records"]
    return result
