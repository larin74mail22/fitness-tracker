"""Строгая проверка входных данных.

Правила: ничего не угадываем. Дата всегда передаётся явно, единицы — только известные,
числа — только числа (строка "80" — ошибка). Сообщения об ошибках — для пересказа пользователю.
"""

import math
import re
from datetime import date, timedelta

from .errors import ValidationError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MIN_DATE = date(1900, 1, 1)
MAX_RANGE_DAYS = 3700
MAX_TEXT = 2000
MAX_SHORT_TEXT = 200
MAX_REQUEST_ID = 100

_MISSING = object()


def check_object(data, allowed, where):
    """Аргумент должен быть словарём без неизвестных полей."""
    if not isinstance(data, dict):
        raise ValidationError(where, "ожидается объект с полями")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValidationError(
            where,
            f"неизвестные поля: {', '.join(unknown)}. Допустимые: {', '.join(sorted(allowed))}",
        )
    return data


def require_any(data, fields, where):
    if not any(data.get(f) is not None for f in fields):
        raise ValidationError(where, f"нужно указать хотя бы одно из полей: {', '.join(fields)}")


def parse_date(value, field, required=True, today=None):
    """Дата 'YYYY-MM-DD'. Не позже завтрашнего дня (запас на часовые пояса)."""
    if value is None:
        if required:
            raise ValidationError(field, "дата обязательна (формат YYYY-MM-DD); уточните её у пользователя")
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError(field, f"неверный формат даты {value!r}; нужен YYYY-MM-DD, например 2026-10-10")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValidationError(field, f"такой даты не существует: {value}") from None
    today = today or date.today()
    if parsed < MIN_DATE:
        raise ValidationError(field, f"слишком ранняя дата: {value}")
    if parsed > today + timedelta(days=1):
        raise ValidationError(field, f"дата {value} в будущем; уточните дату у пользователя")
    return value


def parse_date_range(date_from, date_to):
    """Период включительно. Обе границы обязательны."""
    start = parse_date(date_from, "date_from")
    end = parse_date(date_to, "date_to")
    if start > end:
        raise ValidationError("date_from", f"начало периода {start} позже конца {end}")
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if span > MAX_RANGE_DAYS:
        raise ValidationError("date_to", f"слишком длинный период ({span} дн.); максимум {MAX_RANGE_DAYS}")
    return start, end


def parse_time(value, field):
    """Время 'HH:MM' (24 часа), необязательно."""
    if value is None:
        return None
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ValidationError(field, f"неверный формат времени {value!r}; нужен HH:MM, например 08:30")
    hours, minutes = int(value[:2]), int(value[3:])
    if hours > 23 or minutes > 59:
        raise ValidationError(field, f"такого времени не существует: {value}")
    return value


def parse_number(value, field, *, min_value=None, max_value=None, required=False,
                 integer=False, min_exclusive=False):
    if value is None:
        if required:
            raise ValidationError(field, "значение обязательно")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field, f"ожидается число, получено {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(field, "недопустимое число")
    if integer:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValidationError(field, f"ожидается целое число, получено {value}")
            value = int(value)
    if min_value is not None:
        if min_exclusive and value <= min_value:
            raise ValidationError(field, f"значение должно быть больше {min_value}, получено {value}")
        if not min_exclusive and value < min_value:
            raise ValidationError(field, f"значение должно быть не меньше {min_value}, получено {value}")
    if max_value is not None and value > max_value:
        raise ValidationError(field, f"значение должно быть не больше {max_value}, получено {value}")
    return value


def parse_text(value, field, *, required=False, max_len=MAX_TEXT):
    if value is None:
        if required:
            raise ValidationError(field, "поле обязательно")
        return None
    if not isinstance(value, str):
        raise ValidationError(field, f"ожидается текст, получено {value!r}")
    text = value.strip()
    if not text:
        if required:
            raise ValidationError(field, "поле не может быть пустым")
        return None
    if len(text) > max_len:
        raise ValidationError(field, f"слишком длинный текст ({len(text)} символов, максимум {max_len})")
    return text


def parse_bool(value, field, default=_MISSING):
    if value is None:
        if default is _MISSING:
            raise ValidationError(field, "значение обязательно (true или false)")
        return default
    if not isinstance(value, bool):
        raise ValidationError(field, f"ожидается true или false, получено {value!r}")
    return value


def parse_enum(value, field, allowed, *, required=False, aliases=None):
    if value is None:
        if required:
            raise ValidationError(field, f"поле обязательно; допустимо: {', '.join(allowed)}")
        return None
    if isinstance(value, str):
        key = value.strip().lower()
        if aliases and key in aliases:
            key = aliases[key]
        if key in allowed:
            return key
    raise ValidationError(field, f"недопустимое значение {value!r}; допустимо: {', '.join(allowed)}")


def parse_list(value, field, *, required=False, min_items=0, max_items=200):
    if value is None:
        if required:
            raise ValidationError(field, "список обязателен")
        return []
    if not isinstance(value, list):
        raise ValidationError(field, "ожидается список")
    if len(value) < min_items:
        raise ValidationError(field, f"нужно минимум {min_items} элемент(ов)")
    if len(value) > max_items:
        raise ValidationError(field, f"слишком много элементов ({len(value)}, максимум {max_items})")
    return value


def parse_request_id(value):
    """Необязательный ключ от повторной записи: тот же ключ — та же запись, а не дубликат."""
    return parse_text(value, "client_request_id", max_len=MAX_REQUEST_ID)


def normalize_key(text):
    """Ключ для сравнения названий: нижний регистр, одиночные пробелы, ё=е."""
    return " ".join(text.lower().replace("ё", "е").split())
