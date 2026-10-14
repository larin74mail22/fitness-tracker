"""Получение файла выгрузки Health Connect через MCP (без доступа к произвольным путям на диске).

Два способа, оба отдают байты файла импортёру (health_import.import_bytes):

1. Файл, прикреплённый к сообщению в ChatGPT. Инструмент объявляет параметр файла (_meta "openai/fileParams"),
   ChatGPT передаёт объект {download_url, file_id, mime_type, file_name}; сервер скачивает файл по временной ссылке.
   Принимается только https, без логина в ссылке, только на публичные адреса (не localhost и не локальная сеть),
   не больше MAX_FILE_BYTES; каждый редирект проверяется так же. Модель при этом не пересказывает содержимое файла,
   поэтому так проходят и файлы в десятки мегабайт.
2. Передача частями: start → chunk × N → import. Нужна, если параметр файла недоступен; годится только для небольших
   файлов, потому что каждую часть модель передаёт в аргументах вызова. Перед импортом проверяются полнота (все номера
   частей), размер и SHA-256 целого файла; незавершённая или испорченная передача импорт не запускает.
   Части хранятся в data/health_uploads/<id>/ и удаляются после успешного импорта или через UPLOAD_TTL.
"""

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import health_format
from .errors import TrackerError, ValidationError

MAX_FILE_BYTES = health_format.MAX_FILE_BYTES
MAX_CHUNKS = 1000
MAX_CHUNK_CHARS = 1024 * 1024
UPLOAD_TTL_SEC = 24 * 3600
DOWNLOAD_TIMEOUT_SEC = 60
MAX_REDIRECTS = 5
ENCODINGS = ("utf8", "base64")

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_CHUNK_RE = re.compile(r"chunk-([0-9]{5})\.part")


class TransferError(TrackerError):
    """Файл не удалось получить или передача неполная. Импорт не запускался, база не менялась."""

    code = "transfer_failed"


# ---------- Файл из ChatGPT по временной ссылке ----------

FILE_FIELDS = {"download_url", "file_id", "mime_type", "file_name"}


def _public_host(host):
    """Проверка, что имя указывает только на публичные адреса (защита от запросов в локальную сеть)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransferError(f"Не удалось найти сервер {host!r} для скачивания файла: {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global or address.is_multicast:
            raise TransferError(f"Ссылка на файл ведёт на непубличный адрес ({host}); такие ссылки не принимаются.")


def check_download_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValidationError("file.download_url", "нужна непустая ссылка на файл")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValidationError("file.download_url", "принимаются только ссылки https")
    if not parts.hostname or parts.username or parts.password:
        raise ValidationError("file.download_url", "ссылка без имени сервера или с логином в адресе не принимается")
    try:
        port = parts.port
    except ValueError:
        raise ValidationError("file.download_url", "неверный порт в ссылке") from None
    if port not in (None, 443):
        raise ValidationError("file.download_url", "принимаются только ссылки на стандартный порт https")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise ValidationError("file.download_url", "ссылка на IP-адрес вместо имени сервера не принимается")
    _public_host(parts.hostname)
    return url


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_download_url(urllib.parse.urljoin(req.full_url, newurl))
        except ValidationError as exc:
            raise TransferError(f"Скачивание файла перенаправлено на недопустимый адрес: {exc}") from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_CheckedRedirects.max_redirections = MAX_REDIRECTS


def _download(url):
    opener = urllib.request.build_opener(_CheckedRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "fitness-tracker-import/1"})
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SEC) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                raise TransferError(f"Файл слишком большой ({declared} байт, максимум {MAX_FILE_BYTES}).")
            data = response.read(MAX_FILE_BYTES + 1)
    except TransferError:
        raise
    except urllib.error.HTTPError as exc:
        raise TransferError(f"Сервер с файлом ответил ошибкой {exc.code}. Ссылка могла устареть: прикрепите файл "
                            "к сообщению заново.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransferError(f"Не удалось скачать файл: {reason}. Импорт не запускался.") from None
    if len(data) > MAX_FILE_BYTES:
        raise TransferError(f"Файл слишком большой (больше {MAX_FILE_BYTES} байт).")
    if not data:
        raise TransferError("Скачанный файл пустой.")
    return data


def fetch_file_param(file_obj, downloader=None):
    """Байты и имя файла из параметра файла ChatGPT. downloader подменяется в тестах."""
    if not isinstance(file_obj, dict):
        raise ValidationError("file", "ожидается объект файла {download_url, file_id}")
    unknown = sorted(set(file_obj) - FILE_FIELDS)
    if unknown:
        raise ValidationError("file", f"неизвестные поля: {', '.join(map(str, unknown))}")
    file_id = file_obj.get("file_id")
    if not isinstance(file_id, str) or not file_id or len(file_id) > 512:
        raise ValidationError("file.file_id", "нужен идентификатор файла")
    name = file_obj.get("file_name")
    if name is not None and (not isinstance(name, str) or len(name) > 255):
        raise ValidationError("file.file_name", "имя файла — строка до 255 символов")
    url = check_download_url(file_obj.get("download_url"))
    data = (downloader or _download)(url)
    return data, (Path(name).name if name else f"chatgpt-{file_id}.json")


# ---------- Передача частями ----------

def _dir(base, upload_id):
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValidationError("upload_id", "неверный идентификатор передачи (32 символа 0-9a-f из start_health_upload)")
    path = Path(base) / upload_id
    if not (path / "meta.json").is_file():
        raise TransferError("Передача с таким upload_id не найдена: она завершена, устарела (хранится "
                            f"{UPLOAD_TTL_SEC // 3600} ч) или не начиналась. Начните заново через start_health_upload.")
    return path


def _meta(path):
    try:
        return json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransferError(f"Служебный файл передачи повреждён: {exc}. Начните передачу заново.") from None


def _write_atomic(path, data):
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def cleanup_expired(base, now=None):
    base = Path(base)
    if not base.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for child in base.iterdir():
        if not child.is_dir() or not _UPLOAD_ID_RE.fullmatch(child.name):
            continue
        try:
            started = _meta(child).get("started_at", 0)
        except TransferError:
            started = child.stat().st_mtime
        if now - started > UPLOAD_TTL_SEC:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def start_upload(base, total_bytes, sha256, chunk_count, encoding="utf8", file_name=None):
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or not 1 <= total_bytes <= MAX_FILE_BYTES:
        raise ValidationError("total_bytes", f"размер файла в байтах, целое 1…{MAX_FILE_BYTES}")
    if not isinstance(sha256, str) or not _SHA_RE.fullmatch(sha256.lower()):
        raise ValidationError("sha256", "SHA-256 всего файла: 64 шестнадцатеричных символа")
    if isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or not 1 <= chunk_count <= MAX_CHUNKS:
        raise ValidationError("chunk_count", f"число частей, целое 1…{MAX_CHUNKS}")
    if encoding not in ENCODINGS:
        raise ValidationError("encoding", f"допустимо: {', '.join(ENCODINGS)}")
    if file_name is not None and (not isinstance(file_name, str) or not file_name or len(file_name) > 255):
        raise ValidationError("file_name", "имя файла — строка до 255 символов")
    cleanup_expired(base)
    upload_id = uuid.uuid4().hex
    path = Path(base) / upload_id
    path.mkdir(parents=True)
    meta = {"upload_id": upload_id, "total_bytes": total_bytes, "sha256": sha256.lower(), "chunk_count": chunk_count,
            "encoding": encoding, "file_name": Path(file_name).name if file_name else None, "started_at": time.time()}
    _write_atomic(path / "meta.json", json.dumps(meta).encode("utf-8"))
    return {"upload_id": upload_id, "chunk_count": chunk_count, "encoding": encoding,
            "expires_in_hours": UPLOAD_TTL_SEC // 3600}


def _received(path):
    return sorted(int(m.group(1)) for m in (_CHUNK_RE.fullmatch(p.name) for p in path.iterdir()) if m)


def put_chunk(base, upload_id, index, data, chunk_sha256=None):
    path = _dir(base, upload_id)
    meta = _meta(path)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < meta["chunk_count"]:
        raise ValidationError("index", f"номер части от 0 до {meta['chunk_count'] - 1}")
    if not isinstance(data, str) or not data or len(data) > MAX_CHUNK_CHARS:
        raise ValidationError("data", f"непустая строка до {MAX_CHUNK_CHARS} символов")
    if meta["encoding"] == "base64":
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError("data", "часть не является корректным base64") from None
    else:
        try:
            raw = data.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError("data", "часть содержит символы, которые нельзя записать в UTF-8") from None
    if chunk_sha256 is not None:
        if not isinstance(chunk_sha256, str) or not _SHA_RE.fullmatch(chunk_sha256.lower()):
            raise ValidationError("chunk_sha256", "64 шестнадцатеричных символа")
        if hashlib.sha256(raw).hexdigest() != chunk_sha256.lower():
            raise TransferError(f"Часть {index} повреждена: SHA-256 не совпадает. Передайте эту часть заново.")
    target = path / f"chunk-{index:05d}.part"
    duplicate = False
    if target.exists():
        if target.read_bytes() != raw:
            raise TransferError(f"Часть {index} уже получена с другим содержимым. Передача испорчена: начните заново "
                                "через start_health_upload.")
        duplicate = True
    else:
        _write_atomic(target, raw)
    received = _received(path)
    size = sum((path / f"chunk-{i:05d}.part").stat().st_size for i in received)
    if size > meta["total_bytes"]:
        raise TransferError(f"Получено больше байт ({size}), чем объявлено ({meta['total_bytes']}). Начните заново.")
    return {"upload_id": upload_id, "index": index, "duplicate": duplicate, "received_chunks": len(received),
            "chunk_count": meta["chunk_count"], "received_bytes": size, "total_bytes": meta["total_bytes"],
            "complete": len(received) == meta["chunk_count"]}


def assemble(base, upload_id):
    """Собрать файл. Ошибка, если передача неполная, размер или SHA-256 не совпадают. Возвращает (байты, имя)."""
    path = _dir(base, upload_id)
    meta = _meta(path)
    received = set(_received(path))
    missing = [i for i in range(meta["chunk_count"]) if i not in received]
    if missing:
        shown = ", ".join(map(str, missing[:50])) + (" …" if len(missing) > 50 else "")
        raise TransferError(f"Передача не завершена: нет частей {shown} (получено {len(received)} из "
                            f"{meta['chunk_count']}). Импорт не запускался.")
    data = b"".join((path / f"chunk-{i:05d}.part").read_bytes() for i in range(meta["chunk_count"]))
    if len(data) != meta["total_bytes"]:
        raise TransferError(f"Размер собранного файла {len(data)} байт, а объявлено {meta['total_bytes']}. "
                            "Импорт не запускался.")
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise TransferError("SHA-256 собранного файла не совпадает с объявленным: части перепутаны или повреждены. "
                            "Импорт не запускался.")
    return data, meta.get("file_name") or f"upload-{upload_id}.json"


def discard(base, upload_id):
    if isinstance(upload_id, str) and _UPLOAD_ID_RE.fullmatch(upload_id):
        shutil.rmtree(Path(base) / upload_id, ignore_errors=True)
