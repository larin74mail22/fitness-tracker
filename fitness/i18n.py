"""Язык интерфейса панели и значка в трее: английский (по умолчанию) и русский.

Тексты лежат в словарях fitness/locales/en.py и ru.py по одинаковым ключам. Код интерфейса вызывает t("ключ")
и не проверяет язык сам. Язык текущего запроса хранится в ContextVar: у каждого потока панели он свой.
Если перевода нет, берётся английский текст, а если нет и его — сам ключ (так пропуск сразу виден).

Переводится только интерфейс. Названия инструментов MCP, поля базы, коды и сохранённые записи не меняются,
тексты ошибок ядра (их пересказывает ChatGPT) остаются как есть.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from .locales import en, ru

LANGUAGES = {"en": "EN", "ru": "RU"}
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}   # подсказка на кнопке, всегда на своём языке
DEFAULT_LANGUAGE = "en"
MESSAGES = {"en": en.MESSAGES, "ru": ru.MESSAGES}

_current = ContextVar("ui_language", default=DEFAULT_LANGUAGE)


def normalize(lang):
    """Код языка из настроек или формы; неизвестное значение — язык по умолчанию."""
    lang = str(lang or "").strip().lower()
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def get_language():
    return _current.get()


def set_language(lang):
    """Задать язык для текущего потока (запроса панели). Возвращает токен для ContextVar.reset."""
    return _current.set(normalize(lang))


@contextmanager
def use_language(lang):
    token = set_language(lang)
    try:
        yield
    finally:
        _current.reset(token)


def t(key, *, lang=None, **params):
    """Текст по ключу на текущем (или явно заданном) языке; params подставляются через str.format."""
    lang = normalize(lang) if lang else get_language()
    text = MESSAGES[lang].get(key)
    if text is None:
        text = MESSAGES[DEFAULT_LANGUAGE].get(key, key)
    return text.format(**params) if params else text


def group_thousands(value, lang=None):
    """Целое с разделителем разрядов: 12,345 (en) или 12 345 с неразрывным пробелом (ru)."""
    text = f"{round(value):,}"
    return text.replace(",", "\u00a0") if (normalize(lang) if lang else get_language()) == "ru" else text
