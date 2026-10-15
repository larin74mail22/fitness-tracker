"""HTTP-сервер панели на стандартной библиотеке. Слушает только 127.0.0.1.

Аналитические страницы — только GET/HEAD и только соединение на чтение (readonly.py).
Единственная запись — раздел «Фото»: POST на /photos/upload, /photos/<id>/edit, /photos/<id>/delete
(перенос в корзину), /photos/trash/<id>/restore и /photos/trash/<id>/purge.
Каждый такой запрос проверяет Host, Origin (только эта же панель), секретный токен формы и размер тела.
Ещё одна форма — переключатель языка EN | RU (POST /settings/language) с теми же проверками: язык записывается
в настройки data\launcher.json, а не в базу.

Язык интерфейса читается из настроек на каждом запросе, поэтому переключение действует сразу и без перезапуска.
"""

import hmac
import mimetypes
import re
import secrets
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from email.message import Message
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import __version__, db, health_status, i18n, photo_store, photo_trash
from ..errors import NotFoundError, TrackerError
from ..i18n import t
from ..launcher import config
from . import pages, photo_pages, sync_page
from .readonly import DatabaseUnavailable, readonly_connection

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".heic": "image/heic"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    # same-origin: адрес панели наружу не уходит, а Origin в POST-формах панели приходит настоящий
    # (при no-referrer браузер присылает Origin: null).
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
_PHOTO_RE = re.compile(r"^/photo/(\d{1,9})$")
_PHOTO_PAGE_RE = re.compile(r"^/photos/(\d{1,9})(/delete)?$")
_PHOTO_POST_RE = re.compile(r"^/photos/(\d{1,9})/(edit|delete)$")
_TRASH_FILE_RE = re.compile(r"^/photo-trash/(\d{1,9})$")
_TRASH_PAGE_RE = re.compile(r"^/photos/trash/(\d{1,9})/purge$")
_TRASH_POST_RE = re.compile(r"^/photos/trash/(\d{1,9})/(restore|purge)$")
# Лимит тела запроса: загрузка — несколько файлов разом плюс поля формы; остальные формы — только текст.
MAX_UPLOAD_REQUEST = 100 * 1024 * 1024
MAX_FORM_REQUEST = 64 * 1024
MAX_MULTIPART_PARTS = 40


class BadRequest(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_multipart(body, content_type):
    """Разобрать multipart/form-data. Возвращает (поля {имя: текст}, файлы [(имя поля, имя файла, байты)])."""
    header = Message()
    header["content-type"] = content_type
    boundary = header.get_param("boundary")
    if header.get_content_type() != "multipart/form-data" or not boundary or len(boundary) > 200:
        raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.multipart_expected"))
    delimiter = b"--" + boundary.encode("latin-1")
    chunks = body.split(delimiter)
    if len(chunks) < 2 or len(chunks) > MAX_MULTIPART_PARTS + 2 or not chunks[-1].startswith(b"--"):
        raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.form_too_many_parts"))
    fields, files = {}, []
    for chunk in chunks[1:-1]:
        if not chunk.startswith(b"\r\n") or not chunk.endswith(b"\r\n"):
            raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.form_damaged"))
        head, sep, data = chunk[2:-2].partition(b"\r\n\r\n")
        if not sep:
            raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.form_damaged"))
        part = Message()
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, colon, value = line.partition(":")
            if colon:
                part[key.strip()] = value.strip()
        name = part.get_param("name", header="content-disposition")
        if not name:
            raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.form_unnamed_field"))
        filename = part.get_param("filename", header="content-disposition")
        if filename is None:
            fields[name] = data.decode("utf-8", "replace")
        elif filename or data:          # пустое поле файла (ничего не выбрано) пропускается
            files.append((name, filename, data))
    return fields, files


def log(message):
    print(f"[fitness-dashboard] {message}", file=sys.stderr, flush=True)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port=DEFAULT_PORT, db_path=None):
        # Адрес привязки зафиксирован: только локальный компьютер.
        super().__init__((HOST, port), DashboardHandler)
        self.db_path = db_path
        bound_host, self.port = self.server_address[:2]
        if bound_host != HOST:
            self.server_close()
            raise RuntimeError(f"Сервер привязан к {bound_host}, ожидался {HOST}")
        self.allowed_hosts = {HOST, f"{HOST}:{self.port}", "localhost", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        # Секрет форм раздела «Фото»: меняется при каждом запуске панели, чужие страницы его не знают.
        self.csrf_token = secrets.token_urlsafe(32)

    @property
    def photos_dir(self):
        return db.resolve_db_path(self.db_path).parent / "photos"

    @property
    def trash_dir(self):
        return db.resolve_db_path(self.db_path).parent / "photos_trash"

    @property
    def backup_dir(self):
        return db.resolve_db_path(self.db_path).parent / "backups"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"FitnessDashboard/{__version__}"
    sys_version = ""

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    # ---------- Ответы ----------

    def _send(self, status, body, content_type="text/html; charset=utf-8", head_only=False, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _refuse_method(self):
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, t("error.read_only") + "\n",
                   "text/plain; charset=utf-8", extra={"Allow": "GET, HEAD"})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = lambda self: self._refuse_method()

    def _redirect(self, location):
        self._send(HTTPStatus.SEE_OTHER, "", "text/plain; charset=utf-8", extra={"Location": location})

    def do_HEAD(self):
        self._handle(head_only=True)

    def do_GET(self):
        self._handle(head_only=False)

    # ---------- Маршруты ----------

    def _host_ok(self, head_only=False):
        # Защита от DNS rebinding: чужое имя хоста в запросе — отказ.
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            self._send(HTTPStatus.BAD_REQUEST, t("error.bad_host") + "\n", "text/plain; charset=utf-8",
                       head_only=head_only)
            return False
        return True

    def _begin(self, back):
        """Язык и форма переключателя языка для этого запроса (у каждого потока панели свои)."""
        i18n.set_language(config.read_language(self.server.db_path))
        pages.language_form.set((self.server.csrf_token, back))

    def _handle(self, head_only):
        self._begin(self.path)
        if not self._host_ok(head_only):
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        period = (query.get("period") or [pages.DEFAULT_PERIOD])[0]
        if period not in pages.PERIODS:
            period = pages.DEFAULT_PERIOD

        try:
            photo = _PHOTO_RE.match(url.path)
            if photo:
                self._serve_photo(int(photo.group(1)), head_only)
                return
            trashed = _TRASH_FILE_RE.match(url.path)
            if trashed:
                self._serve_trash_photo(int(trashed.group(1)), head_only)
                return
            if url.path.startswith("/photos/"):
                self._photo_get(url.path, query, period, head_only)
                return
            if url.path == "/sync":
                self._send(HTTPStatus.OK, sync_page.sync_page(health_status.sync_status(self.server.db_path)),
                           head_only=head_only)
                return
            routes = {
                "/": lambda c: pages.overview_page(c, period),
                "/measurements": lambda c: pages.measurements_page(c, period, (query.get("type") or [None])[0]),
                "/workouts": lambda c: pages.workouts_page(c, period, (query.get("exercise") or [None])[0]),
                "/nutrition": lambda c: pages.nutrition_page(c, period),
                "/alcohol": lambda c: pages.alcohol_page(c, period),
                "/photos": lambda c: photo_pages.gallery_page(
                    c, period, photo_pages.ok_notice((query.get("ok") or [""])[0], (query.get("n") or [""])[0]),
                    _int_or_none((query.get("mark") or [""])[0])),
                "/health": lambda c: pages.health_page(c, period),
            }
            render = routes.get(url.path)
            if render is None:
                self._send(HTTPStatus.NOT_FOUND, pages.not_found_page(), head_only=head_only)
                return
            try:
                with readonly_connection(self.server.db_path) as conn:
                    html = render(conn)
            except DatabaseUnavailable as exc:
                html = pages.unavailable_page(str(exc))
            self._send(HTTPStatus.OK, html, head_only=head_only)
        except Exception as exc:
            log(f"error on {url.path}: {exc!r}\n{traceback.format_exc()}")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       pages.message_page(t("error.title"), t("error.internal"), t("error.internal_details")),
                       head_only=head_only)

    # ---------- Раздел «Фото» ----------

    def _render_readonly(self, render):
        """HTML страницы по соединению на чтение; None — фото не найдено."""
        try:
            with readonly_connection(self.server.db_path) as conn:
                return render(conn)
        except DatabaseUnavailable as exc:
            return pages.unavailable_page(str(exc))

    def _send_page(self, html, head_only=False, status=HTTPStatus.OK):
        if html is None:
            self._send(HTTPStatus.NOT_FOUND, photo_pages.missing_photo_page(), head_only=head_only)
        else:
            self._send(status, html, head_only=head_only)

    def _photo_get(self, path, query, period, head_only):
        token = self.server.csrf_token
        if path == "/photos/upload":
            view = (query.get("view") or [None])[0]
            self._send(HTTPStatus.OK, photo_pages.upload_page(period, token, values={"view": view}),
                       head_only=head_only)
            return
        if path == "/photos/trash":
            notice = photo_pages.ok_notice((query.get("ok") or [""])[0])
            self._send(HTTPStatus.OK, self._render_readonly(lambda c: photo_pages.trash_page(
                c, period, token, self.server.trash_dir, notice=notice)), head_only=head_only)
            return
        purge = _TRASH_PAGE_RE.match(path)
        if purge:
            html = self._render_readonly(lambda c: photo_pages.purge_page(
                c, int(purge.group(1)), period, token, self.server.trash_dir))
            if html is None:
                self._send(HTTPStatus.NOT_FOUND, photo_pages.missing_trash_page(), head_only=head_only)
            else:
                self._send(HTTPStatus.OK, html, head_only=head_only)
            return
        if path == "/photos/compare":
            size = (query.get("size") or ["m"])[0]
            self._send_page(self._render_readonly(
                lambda c: photo_pages.compare_page(c, query.get("id") or [], period, size)), head_only)
            return
        match = _PHOTO_PAGE_RE.match(path)
        if match is None:
            self._send(HTTPStatus.NOT_FOUND, pages.not_found_page(), head_only=head_only)
            return
        photo_id = int(match.group(1))
        if match.group(2):
            def render(c):
                return photo_pages.delete_page(c, photo_id, period, token)
        else:
            notice = photo_pages.ok_notice((query.get("ok") or [""])[0])

            def render(c):
                return photo_pages.photo_page(c, photo_id, period, token, notice=notice)
        self._send_page(self._render_readonly(render), head_only)

    def do_POST(self):
        self._begin("/photos")
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        if url.path == "/settings/language":
            self._post_language()
            return
        match = _PHOTO_POST_RE.match(url.path)
        trash = _TRASH_POST_RE.match(url.path)
        if url.path != "/photos/upload" and match is None and trash is None:
            self._refuse_method()
            return
        try:
            fields, files = self._read_form(upload=url.path == "/photos/upload")
            period = fields.get("period") if fields.get("period") in pages.PERIODS else pages.DEFAULT_PERIOD
            if trash is not None:
                self._post_trash(int(trash.group(1)), trash.group(2), fields, period)
            elif match is None:
                self._post_upload(fields, files, period)
            elif match.group(2) == "edit":
                self._post_edit(int(match.group(1)), fields, period)
            else:
                self._post_delete(int(match.group(1)), fields, period)
        except BadRequest as exc:
            self.close_connection = True
            self._send(exc.status, pages.message_page(t("error.title"), t("error.rejected"), str(exc), "/photos",
                                                      ("/photos", t("photos.to_gallery"))))
        except Exception as exc:
            log(f"error on POST {url.path}: {exc!r}\n{traceback.format_exc()}")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       pages.message_page(t("error.title"), t("error.internal"), t("error.internal_nothing_saved"),
                                          "/photos", ("/photos", t("photos.to_gallery"))))

    def _post_language(self):
        """Переключатель EN | RU: сохранить язык в настройках и вернуться на ту же страницу."""
        try:
            fields, _ = self._read_form(upload=False)
            lang = fields.get("lang", "")
            if lang not in i18n.LANGUAGES:
                raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.unknown_language"))
            try:
                config.save_language(lang, self.server.db_path)
            except (OSError, ValueError) as exc:
                log(f"language not saved: {exc}")
                raise BadRequest(HTTPStatus.INTERNAL_SERVER_ERROR, t("error.language_not_saved", error=exc)) from None
        except BadRequest as exc:
            self.close_connection = True
            self._send(exc.status, pages.message_page(t("error.title"), t("error.rejected"), str(exc), "",
                                                      ("/", t("nav.overview"))))
            return
        self._redirect(_local_path(fields.get("next")))

    def _read_form(self, upload):
        """Проверить источник, размер и токен формы, прочитать тело."""
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin not in self.server.allowed_origins:
            raise BadRequest(HTTPStatus.FORBIDDEN, t("error.foreign_origin"))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise BadRequest(HTTPStatus.LENGTH_REQUIRED, t("error.no_length")) from None
        limit = MAX_UPLOAD_REQUEST if upload else MAX_FORM_REQUEST
        if length < 0 or length > limit:
            # Тело не читается: соединение закрывается после ответа.
            raise BadRequest(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             t("error.too_large", total=limit // (1024 * 1024) or 1,
                               file=photo_store.MAX_FILE_BYTES // (1024 * 1024)))
        body = self.rfile.read(length)
        if len(body) != length:
            raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.truncated"))
        content_type = self.headers.get("Content-Type", "")
        if upload:
            fields, files = parse_multipart(body, content_type)
        else:
            if not content_type.startswith("application/x-www-form-urlencoded"):
                raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.urlencoded_expected"))
            parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            fields, files = {k: v[0] for k, v in parsed.items()}, []
        if not hmac.compare_digest(fields.get("csrf", "").encode(), self.server.csrf_token.encode()):
            raise BadRequest(HTTPStatus.FORBIDDEN, t("error.stale_form"))
        return fields, files

    def _write_connection(self):
        # Обычное открытие базы: не создаёт её и не обновляет устаревшую схему.
        return db.open_database(self.server.db_path, create=False)

    def _post_upload(self, fields, files, period):
        uploads = []
        for field, filename, data in files:
            view = field[5:] if field.startswith("file_") else None
            if view not in photo_store.PHOTO_VIEWS:
                raise BadRequest(HTTPStatus.BAD_REQUEST, t("error.unknown_file_field"))
            uploads.append((view, filename, data))
        values = {"taken_on": fields.get("taken_on"), "notes": fields.get("notes")}
        try:
            conn = self._write_connection()
            try:
                result = photo_store.save_uploads(conn, uploads, fields.get("taken_on") or None,
                                                  fields.get("notes"), photos_dir=self.server.photos_dir)
            finally:
                conn.close()
        except TrackerError as exc:
            self._send(HTTPStatus.BAD_REQUEST,
                       photo_pages.upload_page(period, self.server.csrf_token, error=str(exc), values=values))
            return
        self._redirect("/photos?" + urlencode({"period": period, "ok": "uploaded", "n": len(result["ids"])}))

    def _post_edit(self, photo_id, fields, period):
        values = {k: fields.get(k, "") for k in ("taken_on", "view", "notes")}
        try:
            conn = self._write_connection()
            try:
                photo_store.update_photo(conn, photo_id, fields.get("taken_on") or None, fields.get("view") or None,
                                         fields.get("notes"), expected_updated_at=fields.get("updated_at") or None)
            finally:
                conn.close()
        except NotFoundError:
            self._send_page(None)
            return
        except TrackerError as exc:
            html = self._render_readonly(lambda c: photo_pages.photo_page(
                c, photo_id, period, self.server.csrf_token, error=str(exc), values=values))
            self._send_page(html, status=HTTPStatus.BAD_REQUEST)
            return
        self._redirect(f"/photos/{photo_id}?" + urlencode({"period": period, "ok": "saved"}))

    def _post_delete(self, photo_id, fields, period):
        try:
            conn = self._write_connection()
            try:
                result = photo_trash.move_to_trash(conn, photo_id, fields.get("code"), photos_dir=self.server.photos_dir,
                                                   trash_dir=self.server.trash_dir, backup_dir=self.server.backup_dir)
            finally:
                conn.close()
        except NotFoundError:
            self._send_page(None)
            return
        except TrackerError as exc:
            html = self._render_readonly(lambda c: photo_pages.delete_page(
                c, photo_id, period, self.server.csrf_token, error=str(exc)))
            self._send_page(html, status=HTTPStatus.BAD_REQUEST)
            return
        if result["warning"]:
            log(result["warning"])
        self._redirect("/photos?" + urlencode({"period": period, "ok": "deleted"}))

    def _post_trash(self, trash_id, action, fields, period):
        try:
            conn = self._write_connection()
            try:
                if action == "restore":
                    result = photo_trash.restore(conn, trash_id, photos_dir=self.server.photos_dir,
                                                 trash_dir=self.server.trash_dir)
                else:
                    result = photo_trash.purge(conn, trash_id, fields.get("code"), trash_dir=self.server.trash_dir,
                                               backup_dir=self.server.backup_dir)
            finally:
                conn.close()
        except NotFoundError:
            self._send(HTTPStatus.NOT_FOUND, photo_pages.missing_trash_page())
            return
        except TrackerError as exc:
            token, trash_dir = self.server.csrf_token, self.server.trash_dir
            if action == "purge":
                html = self._render_readonly(lambda c: photo_pages.purge_page(
                    c, trash_id, period, token, trash_dir, error=str(exc)))
            else:
                html = None
            if html is None:
                html = self._render_readonly(lambda c: photo_pages.trash_page(
                    c, period, token, trash_dir, error=str(exc)))
            self._send(HTTPStatus.BAD_REQUEST, html)
            return
        if result["warning"]:
            log(result["warning"])
        if action == "restore":
            self._redirect(f"/photos/{result['photo_id']}?" + urlencode({"period": period, "ok": "restored"}))
        else:
            self._redirect("/photos/trash?" + urlencode({"period": period, "ok": "purged"}))

    def _serve_photo(self, photo_id, head_only):
        try:
            with readonly_connection(self.server.db_path) as conn:
                row = conn.execute("SELECT file_path FROM photos WHERE id = ?", (photo_id,)).fetchone()
        except DatabaseUnavailable:
            row = None
        if row is None:
            self._send(HTTPStatus.NOT_FOUND, t("photos.not_found") + "\n", "text/plain; charset=utf-8",
                       head_only=head_only)
            return
        base = Path(self.server.photos_dir).resolve()
        target = (base / Path(*row["file_path"].split("/"))).resolve()
        content_type = PHOTO_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        if base not in target.parents or not target.is_file() or content_type not in PHOTO_TYPES.values():
            self._send(HTTPStatus.NOT_FOUND, t("photos.file_not_found") + "\n", "text/plain; charset=utf-8",
                       head_only=head_only)
            return
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, head_only=head_only)


    def _serve_trash_photo(self, trash_id, head_only):
        try:
            with readonly_connection(self.server.db_path) as conn:
                row = conn.execute("SELECT * FROM photo_trash WHERE id = ?", (trash_id,)).fetchone()
        except DatabaseUnavailable:
            row = None
        target = photo_trash.trash_file(dict(row), self.server.trash_dir) if row is not None else None
        content_type = PHOTO_TYPES.get(target.suffix.lower()) if target is not None else None
        if content_type is None:
            self._send(HTTPStatus.NOT_FOUND, t("photos.file_not_found") + "\n", "text/plain; charset=utf-8",
                       head_only=head_only)
            return
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, head_only=head_only)


def _local_path(value):
    """Адрес возврата после переключения языка: только путь этой панели, иначе главная."""
    value = value or ""
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(c in value for c in "\r\n"):
        return "/"
    return value


def _int_or_none(value):
    return int(value) if value.isdigit() and len(value) <= 9 else None


def make_server(port=DEFAULT_PORT, db_path=None):
    return DashboardServer(port=port, db_path=db_path)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="FITNESS Tracker: локальная веб-панель")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"порт (по умолчанию {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("порт должен быть от 1024 до 65535")
    server = make_server(args.port)
    host, port = server.server_address[:2]
    print(f"Панель: http://{host}:{port}/  (только этот компьютер; запись — только раздел «Фото»)", flush=True)
    print(f"База: {db.resolve_db_path()}", flush=True)
    print("Остановить: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
