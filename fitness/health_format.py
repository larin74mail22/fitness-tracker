"""Разбор и проверка файла выгрузки Health Connect (формат v1). Здесь нет доступа к базе.

Время в файле: ISO 8601, UTC, обязательный суффикс Z, доля секунды от 0 до 9 цифр. Разбор приводит его к
каноническому виду 'YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ' (ровно 9 цифр), поэтому «…:00Z» и «…:00.000Z» — один и тот же
момент, а строковое сравнение совпадает с хронологическим. Подробности: docs/HEALTH_EXPORT_FORMAT.md.

Любая проблема в любой записи отклоняет весь файл (ImportFileError): частичного импорта не бывает.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta

from .errors import ImportFileError

FORMAT_NAME = "fitness-health-export"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RECORDS_PER_TYPE = 200_000
MAX_SAMPLES_PER_RECORD = 10_000
MAX_STAGES_PER_SESSION = 1_000
MAX_DELETED = 1_000_000
MAX_PROBLEMS = 50
MAX_WARNINGS = 20
MAX_ID_LEN = 256
MAX_FUTURE_SKEW = timedelta(hours=24)   # допустимое «опережение» времени записи относительно exported_at
MAX_TEXT_LEN = 2000

STAGES = ("unknown", "awake", "sleeping", "out_of_bed", "light", "deep", "rem", "awake_in_bed")
RECORD_TYPES = ("steps", "heart_rate", "sleep")

# Разбор только через fullmatch: «$» пропускал бы завершающий перевод строки, пробелы по краям тоже недопустимы.
_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z")

HEADER_FIELDS = {"format", "format_version", "phone_id", "sequence", "exported_at", "fallback_utc_offset_min",
                 "period_start", "period_end", "sources", "steps", "heart_rate", "sleep", "deleted"}
_COMMON = {"hc_id", "source", "start", "end", "start_offset_min", "end_offset_min", "last_modified",
           "client_record_id", "client_record_version"}
STEP_FIELDS = _COMMON | {"count"}
HEART_RATE_FIELDS = _COMMON | {"samples"}
SLEEP_FIELDS = _COMMON | {"title", "notes", "stages"}
SOURCE_FIELDS = {"id", "package", "device"}
DEVICE_FIELDS = {"manufacturer", "model", "type"}


class Problems:
    """Собирает проблемы файла (в списке не больше MAX_PROBLEMS, но считает все)."""

    def __init__(self):
        self.items = []
        self.total = 0
        self.time_limit = None      # верхняя граница времени записей (exported_at + допуск), None — не проверять
        self.bad_records = {t: set() for t in RECORD_TYPES}

    def add(self, path, message):
        self.total += 1
        if len(self.items) < MAX_PROBLEMS:
            self.items.append((path, message))
        m = re.match(r"^(steps|heart_rate|sleep)\[([0-9]+)\]", path)
        if m:
            self.bad_records[m.group(1)].add(int(m.group(2)))

    def __bool__(self):
        return self.total > 0

    def rejected(self):
        return {t: len(ids) for t, ids in self.bad_records.items()}


class ParsedFile:
    def __init__(self, header, sources, records, deleted, warnings, sha256, size):
        self.header = header
        self.sources = sources          # id в файле -> описание источника
        self.records = records          # тип -> список нормализованных записей
        self.deleted = deleted          # список hc_id
        self.warnings = warnings
        self.sha256 = sha256
        self.size = size


# ---------- Низкоуровневые проверки ----------

def parse_timestamp(value, path, problems):
    """Каноническое время 'YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ' или None (проблема добавлена)."""
    if not isinstance(value, str):
        problems.add(path, f"время должно быть строкой, получено {_show(value)}")
        return None
    m = _TS_RE.fullmatch(value)
    if not m:
        problems.add(path, f"неверный формат времени {value!r}; нужен ISO 8601 в UTC с суффиксом Z, "
                           "например 2026-10-10T07:05:00.123Z (доля секунды необязательна, до 9 цифр; пробелы и переводы строк недопустимы)")
        return None
    y, mo, d, hh, mi, ss, frac = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss))
    except ValueError:
        problems.add(path, f"такого момента не существует: {value}")
        return None
    if not 1970 <= int(y) <= 2100:
        problems.add(path, f"год вне диапазона 1970–2100: {value}")
        return None
    return f"{y}-{mo}-{d}T{hh}:{mi}:{ss}.{(frac or '').ljust(9, '0')}Z"


def _show(value):
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _int(value, path, problems, low, high, what="значение"):
    if isinstance(value, bool) or not isinstance(value, int):
        problems.add(path, f"{what} должно быть целым числом, получено {_show(value)}")
        return None
    if not low <= value <= high:
        problems.add(path, f"{what} вне диапазона {low}…{high}: {value}")
        return None
    return value


def _offset(value, path, problems):
    if value is None:
        return None
    return _int(value, path, problems, -840, 840, "смещение пояса в минутах")


def _bad_chars(value):
    """Причина, по которой строку нельзя сохранить, или None. NUL обрезает строку в SQLite (ломает CHECK length),
    непарный суррогат не кодируется в UTF-8 (ломает запись в базу)."""
    if "\x00" in value:
        return "символ NUL (U+0000)"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "непарный суррогат Unicode (например U+D800), такой текст нельзя сохранить в UTF-8"
    return None


def _names(keys):
    """Имена полей для сообщений: непечатаемые и суррогатные символы экранируются."""
    return ", ".join(ascii(str(k))[1:-1] for k in keys)


def _future(stamp, path, problems):
    """True (и проблема), если время позже exported_at больше чем на допуск."""
    limit = problems.time_limit
    if limit is not None and stamp is not None and stamp > limit:
        problems.add(path, f"время {stamp} позже времени экспорта файла (exported_at) больше чем на "
                           f"{int(MAX_FUTURE_SKEW.total_seconds() // 3600)} ч: так не бывает; проверьте часы телефона "
                           "и приложение-мост")
        return True
    return False


def _text(value, path, problems, required=False, max_len=MAX_TEXT_LEN):
    if value is None:
        if required:
            problems.add(path, "поле обязательно")
        return None
    if not isinstance(value, str):
        problems.add(path, f"нужен текст, получено {_show(value)}")
        return None
    why = _bad_chars(value)
    if why:
        problems.add(path, f"строка содержит недопустимый символ: {why}")
        return None
    if required and not value.strip():
        problems.add(path, "поле не может быть пустым")
        return None
    if len(value) > max_len:
        problems.add(path, f"слишком длинный текст ({len(value)} символов, максимум {max_len})")
        return None
    return value


def _object(value, path, problems, allowed, required=()):
    if not isinstance(value, dict):
        problems.add(path, f"ожидается объект, получено {_show(value)}")
        return None
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        problems.add(path, f"неизвестные поля: {_names(unknown)}")
    missing = [f for f in required if f not in value]
    if missing:
        problems.add(path, f"нет обязательных полей: {_names(missing)}")
    return value


def _list(value, path, problems, limit):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.add(path, f"ожидается список, получено {_show(value)}")
        return []
    if len(value) > limit:
        problems.add(path, f"слишком много элементов ({len(value)}, максимум {limit})")
        return []
    return value


def local_date(canonical_ts, offset_min):
    """Местная дата момента по смещению пояса в минутах."""
    t = canonical_ts
    moment = datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), int(t[17:19]))
    return (moment + timedelta(minutes=offset_min)).date().isoformat()


def payload_hash(kind, fields):
    """SHA-256 канонического JSON содержимого записи (без hc_id и last_modified)."""
    text = json.dumps({"kind": kind, **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- Разбор JSON ----------

def _decode(data):
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError([("файл", f"слишком большой ({len(data)} байт, максимум {MAX_FILE_BYTES})")])
    if not data.strip():
        raise ImportFileError([("файл", "файл пустой")])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError([("файл", "файл не в кодировке UTF-8")]) from None

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"повторяющийся ключ {key!r}")
            result[key] = value
        return result

    def no_constants(name):
        raise ValueError(f"недопустимое значение {name}")

    try:
        return json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constants)
    except json.JSONDecodeError as exc:
        raise ImportFileError([("файл", f"это не JSON: {exc.msg} (строка {exc.lineno}, столбец {exc.colno})")]) from None
    except (ValueError, RecursionError) as exc:
        raise ImportFileError([("файл", f"некорректный JSON: {exc}")]) from None


def peek_order(data):
    """(phone_id, sequence) из заголовка для сортировки файлов; None, если заголовок не читается."""
    try:
        doc = _decode(data)
    except ImportFileError:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("phone_id"), str) and isinstance(doc.get("sequence"), int) \
            and not isinstance(doc.get("sequence"), bool):
        return doc["phone_id"], doc["sequence"]
    return None


# ---------- Заголовок и источники ----------

def _parse_header(doc, problems):
    """Возвращает заголовок или None. Для журнала неудач нужен корректный заголовок целиком."""
    before = problems.total
    header = {}
    if doc.get("format") != FORMAT_NAME:
        problems.add("format", f"ожидается {FORMAT_NAME!r}, получено {_show(doc.get('format'))}")
    version = doc.get("format_version")
    if isinstance(version, bool) or version != FORMAT_VERSION or not isinstance(version, int):
        problems.add("format_version", f"поддерживается только версия формата {FORMAT_VERSION}, получено {_show(version)}")
    header["format_version"] = version
    header["phone_id"] = _text(doc.get("phone_id"), "phone_id", problems, required=True, max_len=100)
    header["sequence"] = _int(doc.get("sequence"), "sequence", problems, 1, 2**53, "номер файла")
    stamp = parse_timestamp(doc.get("exported_at"), "exported_at", problems) if "exported_at" in doc else None
    if "exported_at" not in doc:
        problems.add("exported_at", "поле обязательно")
    header["exported_at"] = stamp[:19] + "Z" if stamp else None       # в журнале время экспорта хранится до секунды
    header["exported_at_full"] = stamp
    header["fallback_offset"] = _offset(doc.get("fallback_utc_offset_min"), "fallback_utc_offset_min", problems)
    header["period_start"], header["period_end"] = _parse_period(doc, problems)
    return header if problems.total == before else None


def _parse_period(doc, problems):
    """Необязательный период чтения файла (period_start, period_end): оба или ни одного, начало раньше конца.
    По нему импортёр понимает, перекрывает ли файл период пропущенных файлов (health_import._check_order)."""
    present = [name for name in ("period_start", "period_end") if doc.get(name) is not None]
    if not present:
        return None, None
    if len(present) == 1:
        missing = "period_end" if present == ["period_start"] else "period_start"
        problems.add(missing, "период файла задаётся двумя полями: period_start и period_end")
        return None, None
    start = parse_timestamp(doc["period_start"], "period_start", problems)
    end = parse_timestamp(doc["period_end"], "period_end", problems)
    if start is None or end is None:
        return None, None
    if end <= start:
        problems.add("period_end", f"конец периода {end} не позже начала {start}")
        return None, None
    return start, end


def _parse_sources(doc, problems):
    sources = {}
    for i, raw in enumerate(_list(doc.get("sources"), "sources", problems, 1000)):
        path = f"sources[{i}]"
        if _object(raw, path, problems, SOURCE_FIELDS, required=("id", "package")) is None:
            continue
        sid = _text(raw.get("id"), f"{path}.id", problems, required=True, max_len=100)
        package = _text(raw.get("package"), f"{path}.package", problems, required=True, max_len=MAX_ID_LEN)
        device = {"manufacturer": None, "model": None, "type": None}
        raw_device = raw.get("device")
        if raw_device is not None and _object(raw_device, f"{path}.device", problems, DEVICE_FIELDS) is not None:
            for name in DEVICE_FIELDS:
                device[name] = _text(raw_device.get(name), f"{path}.device.{name}", problems, max_len=100)
        if sid is None or package is None:
            continue
        if sid in sources:
            problems.add(f"{path}.id", f"источник {sid!r} описан дважды")
            continue
        key = "|".join(["health_connect", package, device["manufacturer"] or "", device["model"] or "",
                        device["type"] or ""])
        sources[sid] = {"key": key, "package": package, **device}
    return sources


# ---------- Записи ----------

def _common(raw, path, problems, kind, allowed, sources, fallback, offset_for_date):
    """Общие поля записи. Возвращает словарь или None, если запись не годится."""
    if _object(raw, path, problems, allowed, required=("hc_id", "source", "start", "end", "last_modified")) is None:
        return None
    ok = True
    hc_id = _text(raw.get("hc_id"), f"{path}.hc_id", problems, required=True, max_len=MAX_ID_LEN)
    source = raw.get("source")
    if not isinstance(source, str) or source not in sources:
        if "source" in raw:
            problems.add(f"{path}.source", f"источник {_show(source)} не описан в списке sources")
        ok = False
    start = parse_timestamp(raw.get("start"), f"{path}.start", problems) if "start" in raw else None
    end = parse_timestamp(raw.get("end"), f"{path}.end", problems) if "end" in raw else None
    modified = parse_timestamp(raw.get("last_modified"), f"{path}.last_modified", problems) if "last_modified" in raw else None
    start_off = _offset(raw.get("start_offset_min"), f"{path}.start_offset_min", problems)
    end_off = _offset(raw.get("end_offset_min"), f"{path}.end_offset_min", problems)
    client_id = _text(raw.get("client_record_id"), f"{path}.client_record_id", problems, max_len=MAX_ID_LEN)
    client_version = raw.get("client_record_version")
    if client_version is not None:
        client_version = _int(client_version, f"{path}.client_record_version", problems, 0, 2**63 - 1, "версия записи")
    if None in (hc_id, start, end, modified) or not ok:
        return None
    if any([_future(start, f"{path}.start", problems), _future(end, f"{path}.end", problems),
            _future(modified, f"{path}.last_modified", problems)]):
        return None
    if kind in ("steps", "stages") and not end > start:
        problems.add(f"{path}.end", "конец должен быть строго позже начала")
        return None
    if kind != "steps" and end < start:
        problems.add(f"{path}.end", "конец не может быть раньше начала")
        return None
    chosen = {"start": start_off, "end": end_off}[offset_for_date]
    if chosen is None:
        chosen = fallback
    if chosen is None:
        problems.add(f"{path}.{offset_for_date}_offset_min",
                     "нет смещения часового пояса: укажите его в записи или fallback_utc_offset_min в заголовке "
                     "(местную дату угадывать нельзя)")
        return None
    return {"hc_id": hc_id, "source": source, "start": start, "end": end, "start_offset_min": start_off,
            "end_offset_min": end_off, "last_modified": modified, "client_record_id": client_id,
            "client_record_version": client_version,
            "local_date": local_date(start if offset_for_date == "start" else end, chosen)}


def _core(rec):
    return {"start": rec["start"], "end": rec["end"], "so": rec["start_offset_min"], "eo": rec["end_offset_min"],
            "cid": rec["client_record_id"], "cv": rec["client_record_version"], "ld": rec["local_date"]}


def _parse_steps(items, sources, fallback, problems):
    result = []
    for i, raw in enumerate(items):
        path = f"steps[{i}]"
        rec = _common(raw, path, problems, "steps", STEP_FIELDS, sources, fallback, "start")
        count = _int(raw.get("count"), f"{path}.count", problems, 1, 1_000_000, "число шагов") \
            if isinstance(raw, dict) and "count" in raw else None
        if isinstance(raw, dict) and "count" not in raw:
            problems.add(f"{path}.count", "поле обязательно")
        if rec is None or count is None:
            continue
        rec["count"] = count
        rec["payload_hash"] = payload_hash("steps", {**_core(rec), "count": count})
        result.append(rec)
    return result


def _parse_heart_rate(items, sources, fallback, problems, warnings):
    result = []
    for i, raw in enumerate(items):
        path = f"heart_rate[{i}]"
        rec = _common(raw, path, problems, "heart_rate", HEART_RATE_FIELDS, sources, fallback, "start")
        samples, seen, good = [], set(), True
        if isinstance(raw, dict):
            if "samples" not in raw:
                problems.add(f"{path}.samples", "поле обязательно (может быть пустым списком)")
                good = False
            for j, s in enumerate(_list(raw.get("samples"), f"{path}.samples", problems, MAX_SAMPLES_PER_RECORD)):
                sp = f"{path}.samples[{j}]"
                if _object(s, sp, problems, {"time", "bpm"}, required=("time", "bpm")) is None:
                    good = False
                    continue
                t = parse_timestamp(s.get("time"), f"{sp}.time", problems) if "time" in s else None
                bpm = _int(s.get("bpm"), f"{sp}.bpm", problems, 1, 300, "пульс") if "bpm" in s else None
                if t is None or bpm is None:
                    good = False
                    continue
                if _future(t, f"{sp}.time", problems):
                    good = False
                    continue
                if t in seen:
                    problems.add(f"{sp}.time", f"отсчёт на момент {t} уже есть в этой записи")
                    good = False
                    continue
                seen.add(t)
                samples.append((t, bpm))
        if rec is None or not good:
            continue
        samples.sort()
        if samples and (samples[0][0] < rec["start"] or samples[-1][0] > rec["end"]) and len(warnings) < MAX_WARNINGS:
            warnings.append(f"{path}: время отсчётов выходит за границы записи (сохранено как есть)")
        rec["samples"] = samples
        rec["payload_hash"] = payload_hash("heart_rate", {**_core(rec), "samples": samples})
        result.append(rec)
    return result


def _parse_sleep(items, sources, fallback, problems, warnings):
    result = []
    for i, raw in enumerate(items):
        path = f"sleep[{i}]"
        rec = _common(raw, path, problems, "sleep", SLEEP_FIELDS, sources, fallback, "end")
        title = _text(raw.get("title"), f"{path}.title", problems) if isinstance(raw, dict) else None
        notes = _text(raw.get("notes"), f"{path}.notes", problems) if isinstance(raw, dict) else None
        stages, starts, good = [], set(), True
        if isinstance(raw, dict):
            if "stages" not in raw:
                problems.add(f"{path}.stages", "поле обязательно (может быть пустым списком)")
                good = False
            for j, s in enumerate(_list(raw.get("stages"), f"{path}.stages", problems, MAX_STAGES_PER_SESSION)):
                sp = f"{path}.stages[{j}]"
                if _object(s, sp, problems, {"stage", "start", "end"}, required=("stage", "start", "end")) is None:
                    good = False
                    continue
                stage = s.get("stage")
                if stage not in STAGES:
                    problems.add(f"{sp}.stage", f"неизвестная стадия {_show(stage)}; допустимо: {', '.join(STAGES)}")
                    good = False
                a = parse_timestamp(s.get("start"), f"{sp}.start", problems) if "start" in s else None
                b = parse_timestamp(s.get("end"), f"{sp}.end", problems) if "end" in s else None
                if a is None or b is None or stage not in STAGES:
                    good = False
                    continue
                if any([_future(a, f"{sp}.start", problems), _future(b, f"{sp}.end", problems)]):
                    good = False
                    continue
                if not b > a:
                    problems.add(f"{sp}.end", "конец стадии должен быть строго позже начала")
                    good = False
                    continue
                if a in starts:
                    problems.add(f"{sp}.start", f"стадия с началом {a} уже есть в этой сессии")
                    good = False
                    continue
                starts.add(a)
                stages.append((stage, a, b))
        if rec is None or not good:
            continue
        stages.sort(key=lambda s: (s[1], s[2]))
        if len(warnings) < MAX_WARNINGS:
            outside = stages and (stages[0][1] < rec["start"] or max(s[2] for s in stages) > rec["end"])
            overlap = any(stages[k + 1][1] < stages[k][2] for k in range(len(stages) - 1))
            if outside or overlap:
                warnings.append(f"{path}: стадии сна выходят за границы сессии или перекрываются (сохранено как есть)")
        rec["title"] = title
        rec["notes"] = notes
        rec["stages"] = stages
        rec["payload_hash"] = payload_hash("sleep", {**_core(rec), "title": title, "notes": notes, "stages": stages})
        result.append(rec)
    return result


# ---------- Файл целиком ----------

def parse_export(data):
    """Разобрать и проверить файл (bytes). При любой проблеме — ImportFileError, ничего не импортируется."""
    sha = hashlib.sha256(data).hexdigest()
    doc = _decode(data)
    problems = Problems()
    if not isinstance(doc, dict):
        raise ImportFileError([("файл", "в корне файла должен быть объект JSON")])
    unknown = sorted(set(doc) - HEADER_FIELDS)
    if unknown:
        problems.add("файл", f"неизвестные поля верхнего уровня: {_names(unknown)}")
    header = _parse_header(doc, problems)
    if header and header["exported_at_full"]:
        t = header["exported_at_full"]
        limit = datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), int(t[17:19])) + MAX_FUTURE_SKEW
        problems.time_limit = limit.strftime("%Y-%m-%dT%H:%M:%S") + ".999999999Z"
        _future(header["period_end"], "period_end", problems)
    fallback = _offset(doc.get("fallback_utc_offset_min"), "fallback_utc_offset_min", Problems())
    sources = _parse_sources(doc, problems)
    warnings = []
    records = {
        "steps": _parse_steps(_list(doc.get("steps"), "steps", problems, MAX_RECORDS_PER_TYPE), sources, fallback, problems),
        "heart_rate": _parse_heart_rate(_list(doc.get("heart_rate"), "heart_rate", problems, MAX_RECORDS_PER_TYPE),
                                        sources, fallback, problems, warnings),
        "sleep": _parse_sleep(_list(doc.get("sleep"), "sleep", problems, MAX_RECORDS_PER_TYPE), sources, fallback,
                              problems, warnings),
    }

    deleted, seen_deleted = [], set()
    for i, raw in enumerate(_list(doc.get("deleted"), "deleted", problems, MAX_DELETED)):
        value = _text(raw, f"deleted[{i}]", problems, required=True, max_len=MAX_ID_LEN)
        if value is not None and value not in seen_deleted:
            seen_deleted.add(value)
            deleted.append(value)

    # hc_id должен быть уникален во всём файле и не может одновременно быть записью и удалением
    owner = {}
    for kind in RECORD_TYPES:
        for rec in records[kind]:
            if rec["hc_id"] in owner:
                problems.add(f"{kind}", f"hc_id {rec['hc_id']!r} встречается в файле больше одного раза")
            owner[rec["hc_id"]] = kind
    for hc_id in deleted:
        if hc_id in owner:
            problems.add("deleted", f"hc_id {hc_id!r} одновременно в записях ({owner[hc_id]}) и в списке удалений")

    if problems:
        raise ImportFileError(problems.items, problems.total, header, problems.rejected())
    return ParsedFile(header, sources, records, deleted, warnings, sha, len(data))
