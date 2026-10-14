"""Раздел «Фото» веб-панели: загрузка, галерея по ракурсам, сравнение, правка, удаление, защита.

Панель поднимается на случайном порту 127.0.0.1 с временной базой и временной папкой фото.
Рабочие data/fitness.db и data/photos не используются. Данные и «изображения» выдуманные.
"""

import http.client
import re
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from fitness import db, measurements, photo_store
from fitness.web import app, photo_pages, readonly
from tests.test_dashboard import DashboardTestCase, file_state


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def jpeg(tag=b""):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + tag + bytes(range(256)) + b"\r\n--x\r\n" + b"\xff\xd9"


def png(tag=b""):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + tag + bytes(100)


def webp(tag=b""):
    return b"RIFF\x00\x01\x00\x00WEBPVP8 " + tag + bytes(100)


def multipart(fields, files):
    """fields: {имя: текст}; files: [(имя поля, имя файла, байты)]."""
    boundary = "----test" + uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() +
                   value.encode("utf-8") + b"\r\n")
    for name, filename, data in files:
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                   f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8") + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class PhotoTestCase(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.photos = self.db_path.parent / "photos"
        db.open_database(self.db_path).close()

    def origin(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, body=b"", content_type="application/x-www-form-urlencoded", origin=True, headers=None):
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            hdrs = {"Content-Type": content_type}
            if origin:
                hdrs["Origin"] = self.origin() if origin is True else origin
            hdrs.update(headers or {})
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def upload(self, files, taken_on=None, notes="", csrf=None, origin=True, period="90"):
        fields = {"csrf": self.server.csrf_token if csrf is None else csrf, "period": period,
                  "taken_on": days_ago(1) if taken_on is None else taken_on, "notes": notes}
        body, ctype = multipart(fields, files)
        return self.post("/photos/upload", body, ctype, origin=origin)

    def form(self, path, **fields):
        from urllib.parse import urlencode
        fields.setdefault("csrf", self.server.csrf_token)
        return self.post(path, urlencode(fields).encode())

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM photos ORDER BY id")]
        finally:
            conn.close()

    def files_on_disk(self):
        return sorted(p for p in self.photos.rglob("*") if p.is_file()) if self.photos.exists() else []

    def add_existing(self, name, taken_on, view, data, notes=None):
        """Фото, записанное старым способом: файл положен вручную, запись — через add_photo."""
        self.photos.mkdir(parents=True, exist_ok=True)
        (self.photos / name).write_bytes(data)
        conn = db.open_database(self.db_path)
        try:
            return measurements.add_photo(conn, {"taken_on": taken_on, "file_path": name, "view": view,
                                                 "notes": notes}, photos_dir=self.photos)["id"]
        finally:
            conn.close()

    def sections(self, html):
        """{название секции: [id фото по порядку]}."""
        result = {}
        for label, body in re.findall(r"<section><h3>([^<]+?) <span.*?</h3>(.*?)</section>", html, re.S):
            result[label] = [int(x) for x in re.findall(r'src="/photo/(\d+)"', body)]
        return result


class UploadTests(PhotoTestCase):
    def test_upload_valid_image(self):
        self.start()
        status, headers, _ = self.upload([("file_front", "IMG_0001.jpg", jpeg())], taken_on=days_ago(3),
                                         notes="утро, натощак")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/photos?"))
        self.assertIn("ok=uploaded", headers["Location"])
        [row] = self.rows()
        self.assertEqual((row["taken_on"], row["view"], row["notes"]), (days_ago(3), "front", "утро, натощак"))
        saved = self.photos / Path(*row["file_path"].split("/"))
        self.assertEqual(saved.read_bytes(), jpeg())
        self.assertTrue(saved.name.endswith(".jpg"))
        self.assertNotIn("IMG_0001", saved.name)          # имя генерируется, переименовывать не нужно
        # Галерея обновилась без перезапуска, файл отдаётся
        html = self.get_html(headers["Location"])
        self.assertIn("Фото сохранено", html)
        self.assertEqual(self.sections(html)["Спереди"], [row["id"]])
        status, hdrs, body = self.request(f"/photo/{row['id']}")
        self.assertEqual((status, hdrs["Content-Type"]), (200, "image/jpeg"))

    def test_upload_several_views_at_once(self):
        self.start()
        status, headers, _ = self.upload([("file_front", "a.jpg", jpeg(b"1")), ("file_back", "b.png", png(b"2")),
                                          ("file_side", "c.webp", webp(b"3")), ("file_side", "d.jpg", jpeg(b"4"))])
        self.assertEqual(status, 303)
        self.assertIn("n=4", headers["Location"])
        self.assertEqual([r["view"] for r in self.rows()], ["front", "back", "side", "side"])
        self.assertEqual(sorted(p.suffix for p in self.files_on_disk()), [".jpg", ".jpg", ".png", ".webp"])
        self.assertIn("Сохранено фото: 4", self.get_html(headers["Location"]))

    def test_same_name_twice_does_not_overwrite(self):
        self.start()
        self.assertEqual(self.upload([("file_front", "photo.jpg", jpeg(b"a"))])[0], 303)
        self.assertEqual(self.upload([("file_front", "photo.jpg", jpeg(b"b"))])[0], 303)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["file_path"], rows[1]["file_path"])
        contents = {p.read_bytes() for p in self.files_on_disk()}
        self.assertEqual(contents, {jpeg(b"a"), jpeg(b"b")})

    def test_duplicate_content_refused(self):
        self.start()
        self.assertEqual(self.upload([("file_front", "a.jpg", jpeg())])[0], 303)
        status, _, html = self.upload([("file_back", "copy.jpg", jpeg())])
        self.assertEqual(status, 400)
        self.assertIn("уже загружен ранее", html)
        status, _, html = self.upload([("file_back", "x.jpg", jpeg(b"z")), ("file_side", "y.jpg", jpeg(b"z"))])
        self.assertEqual(status, 400)
        self.assertIn("выбран дважды", html)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.files_on_disk()), 1)

    def test_date_and_fields_validated(self):
        self.start()
        future = (date.today() + timedelta(days=5)).isoformat()
        for taken_on, expected in ((future, "в будущем"), ("", "дата обязательна"), ("2026-02-30", "не существует")):
            with self.subTest(taken_on=taken_on):
                status, _, html = self.upload([("file_front", "a.jpg", jpeg())], taken_on=taken_on)
                self.assertEqual(status, 400)
                self.assertIn(expected, html)
        status, _, html = self.upload([])
        self.assertEqual(status, 400)
        self.assertIn("выберите хотя бы один файл", html)
        status, _, _ = self.upload([("file_top", "a.jpg", jpeg())])
        self.assertEqual(status, 400)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.files_on_disk(), [])

    def test_forbidden_formats_and_bad_files(self):
        self.start()
        bad = [("note.txt", b"just some text, definitely not an image" * 3, "не похож на изображение"),
               ("page.jpg", b"<html><script>alert(1)</script></html>" * 3, "не похож на изображение"),
               ("pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' * 2,
                "не похож на изображение"),
               ("run.exe", b"MZ\x90\x00" + bytes(200), "не похож на изображение"),
               ("phone.heic", b"\x00\x00\x00\x18ftypheic" + bytes(200), "HEIC"),
               ("empty.jpg", b"", "пустой"),
               ("tiny.jpg", b"\xff\xd8\xff\xe0", "слишком маленький")]
        for name, data, expected in bad:
            with self.subTest(name=name):
                status, _, html = self.upload([("file_front", name, data)])
                self.assertEqual(status, 400)
                self.assertIn(expected, html)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.files_on_disk(), [])

    def test_file_size_limit(self):
        self.start()
        with mock.patch.object(photo_store, "MAX_FILE_BYTES", 1000):
            status, _, html = self.upload([("file_front", "big.jpg", jpeg() + bytes(2000))])
        self.assertEqual(status, 400)
        self.assertIn("больше", html)
        self.assertEqual(self.files_on_disk(), [])

    def test_request_size_limit(self):
        self.start()
        with mock.patch.object(app, "MAX_UPLOAD_REQUEST", 2000):
            status, _, html = self.upload([("file_front", "big.jpg", jpeg() + bytes(5000))])
        self.assertEqual(status, 413)
        self.assertIn("Слишком большой запрос", html)
        # Заявленный размер больше лимита: тело даже не читается
        status, _, _ = self.post("/photos/upload", b"", "multipart/form-data; boundary=x",
                                 headers={"Content-Length": str(app.MAX_UPLOAD_REQUEST + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.files_on_disk(), [])

    def test_unsafe_names_stay_inside_photos(self):
        self.start()
        names = ["../../evil.jpg", "..\\..\\evil2.jpg", "C:\\Windows\\evil3.jpg", "/etc/evil4.jpg",
                 "evil5.jpg\x00.txt", "con.jpg", "<b>x</b>.jpg"]
        files = [("file_other", n, jpeg(str(i).encode())) for i, n in enumerate(names)]
        status, _, _ = self.upload(files)
        self.assertEqual(status, 303)
        base = self.photos.resolve()
        for row in self.rows():
            path = (self.photos / Path(*row["file_path"].split("/"))).resolve()
            self.assertIn(base, path.parents)
            self.assertRegex(row["file_path"], r"^\d{4}/\d{4}-\d{2}-\d{2}_other_[0-9a-f]{8}\.jpg$")
        outside = [p for p in self.tmp.rglob("evil*")]
        self.assertEqual(outside, [])
        self.assertEqual(len(self.files_on_disk()), len(names))

    def test_db_failure_leaves_no_files(self):
        self.start()
        real_transaction = photo_store.transaction

        def broken(conn):
            conn.execute("CREATE TEMP TRIGGER fail_photos BEFORE INSERT ON photos "
                         "WHEN (SELECT COUNT(*) FROM photos) >= 1 BEGIN SELECT RAISE(ABORT, 'disk full'); END")
            return real_transaction(conn)

        with mock.patch.object(photo_store, "transaction", broken):
            status, _, _ = self.upload([("file_front", "a.jpg", jpeg(b"1")), ("file_back", "b.jpg", jpeg(b"2"))])
        self.assertEqual(status, 500)
        self.assertEqual(self.rows(), [])                 # первая вставка откатилась вместе со второй
        self.assertEqual(self.files_on_disk(), [])        # созданные файлы удалены

    def test_file_write_failure_cleans_up(self):
        self.start()
        real_new_file = photo_store._new_file
        calls = []

        def flaky(*args):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("нет места на диске")
            return real_new_file(*args)

        with mock.patch.object(photo_store, "_new_file", flaky):
            status, _, _ = self.upload([("file_front", "a.jpg", jpeg(b"1")), ("file_back", "b.jpg", jpeg(b"2"))])
        self.assertEqual(status, 500)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.files_on_disk(), [])

    def test_existing_file_never_overwritten(self):
        self.photos.mkdir(parents=True)
        year = days_ago(1)[:4]
        (self.photos / year).mkdir()
        taken = self.photos / year / f"{days_ago(1)}_front_aaaaaaaa.jpg"
        taken.write_bytes(b"old")
        conn = db.open_database(self.db_path)
        try:
            with mock.patch.object(photo_store.secrets, "token_hex", side_effect=["aaaaaaaa", "bbbbbbbb"]):
                result = photo_store.save_uploads(conn, [("front", "a.jpg", jpeg())], days_ago(1),
                                                  photos_dir=self.photos)
        finally:
            conn.close()
        self.assertEqual(taken.read_bytes(), b"old")
        self.assertTrue(result["records"][0]["file_path"].endswith("_bbbbbbbb.jpg"))


class SecurityTests(PhotoTestCase):
    def test_csrf_origin_and_methods(self):
        self.start()
        files = [("file_front", "a.jpg", jpeg())]
        self.assertEqual(self.upload(files, csrf="")[0], 403)
        self.assertEqual(self.upload(files, csrf="wrong-token")[0], 403)
        self.assertEqual(self.upload(files, origin=False)[0], 403)
        self.assertEqual(self.upload(files, origin="http://evil.example.com")[0], 403)
        self.assertEqual(self.upload(files, origin="null")[0], 403)
        status, headers, _ = self.post("/", b"x=1")
        self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"))
        self.assertEqual(self.post("/photos", b"x=1")[0], 405)
        self.assertEqual(self.post("/measurements", b"x=1")[0], 405)
        self.assertEqual(self.rows(), [])

    def test_get_pages_do_not_write(self):
        pid = self.add_existing("front.jpg", days_ago(2), "front", jpeg())
        self.add_existing("back.jpg", days_ago(1), "back", jpeg(b"b"))
        before = file_state(self.db_path)
        self.start()
        for path in ("/photos", "/photos/upload", f"/photos/{pid}", f"/photos/{pid}/delete",
                     "/photos/compare?id=1&id=2", "/photos/compare?id=1", "/photos/999", "/photos?mark=1"):
            with self.subTest(path=path):
                self.assertIn(self.request(path)[0], (200, 404))
        self.server.shutdown()
        self.assertEqual(file_state(self.db_path), before)
        self.assertEqual(len(self.rows()), 2)

    def test_analytics_layer_stays_readonly(self):
        source = Path(readonly.__file__).read_text(encoding="utf-8")
        self.assertNotIn("photo_store", source)
        self.assertEqual(readonly._ALLOWED_ACTIONS,
                         {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION})
        conn = readonly.connect_readonly(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("INSERT INTO photos (taken_on, file_path) VALUES ('2026-01-01', 'x.jpg')")
        finally:
            conn.close()

    def test_no_scripts_and_csp(self):
        self.add_existing("front.jpg", days_ago(2), "front", jpeg(), notes="<script>alert(1)</script>")
        self.start()
        status, headers, _ = self.request("/photos")
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        for path in ("/photos", "/photos/upload", "/photos/1", "/photos/1/delete"):
            html = self.get_html(path)
            self.assertNotIn("<script", html, path)
            self.assertNotIn("https://", html, path)
        self.assertIn("&lt;script&gt;", self.get_html("/photos"))

    def test_missing_database_not_created_by_upload(self):
        self.start()
        self.db_path.unlink()
        status, _, html = self.upload([("file_front", "a.jpg", jpeg())])
        self.assertEqual(status, 400)
        self.assertIn("База данных не найдена", html)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.files_on_disk(), [])


class GalleryTests(PhotoTestCase):
    def test_categories_and_order(self):
        old_front = self.add_existing("f-old.jpg", days_ago(30), "front", jpeg(b"1"))
        new_front = self.add_existing("f-new.jpg", days_ago(2), "front", jpeg(b"2"))
        mid_front = self.add_existing("f-mid.jpg", days_ago(10), "front", jpeg(b"3"))
        back = self.add_existing("b.jpg", days_ago(5), "back", jpeg(b"4"))
        side = self.add_existing("s.jpg", days_ago(5), "side", jpeg(b"5"))
        other = self.add_existing("o.jpg", days_ago(4), "other", jpeg(b"6"))
        no_view = self.add_existing("legacy.jpg", days_ago(20), None, jpeg(b"7"), notes="старое фото")
        self.start()
        html = self.get_html("/photos")
        self.assertEqual(self.sections(html), {
            "Спереди": [old_front, mid_front, new_front],
            "Сзади": [back],
            "Сбоку": [side],
            "Другое": [no_view, other],
        })
        self.assertIn("ракурс не указан", html)
        self.assertIn("старое фото", html)
        self.assertLess(html.index("<h3>Спереди"), html.index("<h3>Сзади"))
        self.assertLess(html.index("<h3>Сзади"), html.index("<h3>Сбоку"))
        self.assertLess(html.index("<h3>Сбоку"), html.index("<h3>Другое"))

    def test_period_filter_applies_to_all_sections(self):
        self.add_existing("f-old.jpg", days_ago(60), "front", jpeg(b"1"))
        recent = self.add_existing("f-new.jpg", days_ago(3), "front", jpeg(b"2"))
        self.add_existing("b-old.jpg", days_ago(50), "back", jpeg(b"3"))
        self.start()
        sections = self.sections(self.get_html("/photos?period=30"))
        self.assertEqual(sections["Спереди"], [recent])
        self.assertEqual(sections["Сзади"], [])
        html = self.get_html("/photos?period=30")
        self.assertIn("Снимков «Сзади» за этот период нет", html)
        self.assertIn('href="/photos/upload?view=back', html)
        self.assertEqual(len(self.sections(self.get_html("/photos?period=90"))["Сзади"]), 1)

    def test_empty_gallery(self):
        self.start()
        html = self.get_html("/photos")
        self.assertIn("Фото за этот период нет", html)
        self.assertIn("Загрузить фото", html)
        self.assertEqual(self.sections(html), {"Спереди": [], "Сзади": [], "Сбоку": [], "Другое": []})

    def test_group_by_view_keeps_unknown_in_other(self):
        groups = photo_pages.group_by_view([{"view": None, "id": 1}, {"view": "front", "id": 2},
                                            {"view": "weird", "id": 3}])
        self.assertEqual([r["id"] for r in groups["other"]], [1, 3])
        self.assertEqual([r["id"] for r in groups["front"]], [2])

    def test_photo_page_and_mark_for_compare(self):
        pid = self.add_existing("front.jpg", days_ago(2), "front", jpeg(), notes="утро")
        self.start()
        html = self.get_html(f"/photos/{pid}")
        self.assertIn(f'src="/photo/{pid}"', html)
        self.assertIn("утро", html)
        self.assertIn(f"mark={pid}", html)
        gallery = self.get_html(f"/photos?mark={pid}")
        self.assertIn(f'value="{pid}" checked', gallery)
        self.assertEqual(self.request("/photos/999")[0], 404)
        self.assertEqual(self.request("/photos/abc")[0], 404)
        self.assertEqual(self.request("/photos/1/edit")[0], 404)

    def test_existing_routes_still_work(self):
        self.add_existing("front.jpg", days_ago(2), "front", jpeg())
        self.start()
        for path in ("/", "/measurements", "/workouts", "/nutrition", "/alcohol", "/health", "/photos",
                     "/photos/upload", "/photos/upload?view=side"):
            with self.subTest(path=path):
                self.get_html(path)
        self.assertEqual(self.request("/photo/1")[0], 200)


class CompareTests(PhotoTestCase):
    def test_compare_two_photos(self):
        a = self.add_existing("a.jpg", days_ago(30), "front", jpeg(b"1"), notes="старт")
        b = self.add_existing("b.jpg", days_ago(2), "front", jpeg(b"2"), notes="через месяц")
        self.start()
        html = self.get_html(f"/photos/compare?period=90&id={a}&id={b}")
        self.assertLess(html.index(f'src="/photo/{a}"'), html.index(f'src="/photo/{b}"'))
        self.assertIn("старт", html)
        self.assertIn("через месяц", html)
        self.assertIn("Между снимками 28 дн.", html)
        self.assertNotIn("Ракурсы разные", html)
        self.assertIn("Выйти из сравнения", html)
        heights = re.findall(r'style="height:([^"]+)"', html)
        self.assertEqual(len(heights), 2)
        self.assertEqual(heights[0], heights[1])            # одинаковая высота, ширина по пропорциям
        self.assertIn(f"id={b}&amp;id={a}", html)            # «Поменять местами»
        swapped = self.get_html(f"/photos/compare?id={b}&id={a}")
        self.assertLess(swapped.index(f'src="/photo/{b}"'), swapped.index(f'src="/photo/{a}"'))
        large = self.get_html(f"/photos/compare?id={a}&id={b}&size=l")
        self.assertIn("height:85vh", large)

    def test_different_views_warn(self):
        a = self.add_existing("a.jpg", days_ago(3), "front", jpeg(b"1"))
        b = self.add_existing("b.jpg", days_ago(2), "side", jpeg(b"2"))
        self.start()
        html = self.get_html(f"/photos/compare?id={a}&id={b}")
        self.assertIn("Ракурсы разные", html)
        self.assertIn(f'src="/photo/{b}"', html)

    def test_wrong_selection(self):
        a = self.add_existing("a.jpg", days_ago(3), "front", jpeg(b"1"))
        self.add_existing("b.jpg", days_ago(2), "front", jpeg(b"2"))
        self.add_existing("c.jpg", days_ago(1), "front", jpeg(b"3"))
        self.start()
        for query in (f"id={a}", "", "id=1&id=2&id=3", f"id={a}&id={a}", "id=x&id=1"):
            with self.subTest(query=query):
                self.assertIn("отметьте ровно две", self.get_html(f"/photos/compare?{query}"))
        self.assertIn("не найдена", self.get_html(f"/photos/compare?id={a}&id=999"))
        self.assertEqual(photo_pages.parse_compare_ids(["2", "1"]), [2, 1])


class EditDeleteTests(PhotoTestCase):
    def updated_at(self, pid):
        return next(r["updated_at"] for r in self.rows() if r["id"] == pid)

    def test_edit_date_view_comment(self):
        pid = self.add_existing("legacy.jpg", days_ago(20), None, jpeg())
        self.start()
        self.assertIn("не указан", self.get_html(f"/photos/{pid}"))
        status, headers, _ = self.form(f"/photos/{pid}/edit", taken_on=days_ago(19), view="back", notes="вечер",
                                       updated_at=self.updated_at(pid), period="90")
        self.assertEqual(status, 303)
        row = self.rows()[0]
        self.assertEqual((row["taken_on"], row["view"], row["notes"], row["file_path"]),
                         (days_ago(19), "back", "вечер", "legacy.jpg"))
        self.assertEqual((self.photos / "legacy.jpg").read_bytes(), jpeg())
        self.assertIn("Изменения сохранены", self.get_html(headers["Location"]))
        self.assertEqual(self.sections(self.get_html("/photos"))["Сзади"], [pid])

    def test_edit_validation_and_stale_form(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        self.start()
        status, _, html = self.form(f"/photos/{pid}/edit", taken_on="2026-13-01", view="front", notes="")
        self.assertEqual(status, 400)
        self.assertIn("не существует", html)
        status, _, html = self.form(f"/photos/{pid}/edit", taken_on=days_ago(5), view="top", notes="")
        self.assertEqual(status, 400)
        status, _, html = self.form(f"/photos/{pid}/edit", taken_on=days_ago(5), view="front", notes="",
                                    updated_at="2000-01-01T00:00:00Z")
        self.assertEqual(status, 400)
        self.assertIn("изменилось после открытия формы", html)
        self.assertEqual(self.form("/photos/999/edit", taken_on=days_ago(5), view="front")[0], 404)
        self.assertEqual(self.rows()[0]["taken_on"], days_ago(5))

    def test_delete_requires_confirmation_and_makes_backup(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        self.start()
        html = self.get_html(f"/photos/{pid}/delete")
        self.assertIn("Да, переместить в корзину", html)
        code = re.search(r'name="code" value="([0-9A-F]+)"', html).group(1)
        status, _, html = self.form(f"/photos/{pid}/delete", code="WRONG")
        self.assertEqual(status, 400)
        self.assertEqual(len(self.rows()), 1)
        status, _, _ = self.form(f"/photos/{pid}/delete", code=code, csrf="bad")
        self.assertEqual(status, 403)
        self.assertEqual(len(self.rows()), 1)

        status, headers, _ = self.form(f"/photos/{pid}/delete", code=code, period="90")
        self.assertEqual(status, 303)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.photos / "a.jpg").exists())          # файл перенесён в корзину, не удалён
        self.assertEqual((self.db_path.parent / "photos_trash" / "a.jpg").read_bytes(), jpeg())
        backups = list((self.db_path.parent / "backups").glob("*before-delete-photo*.db"))
        self.assertEqual(len(backups), 1)
        self.assertIn("Фото перенесено в корзину", self.get_html(headers["Location"]))
        self.assertEqual(self.request(f"/photos/{pid}/delete")[0], 404)


class StoreTests(PhotoTestCase):
    def test_detect_format(self):
        self.assertEqual(photo_store.detect_format(jpeg()), "jpeg")
        self.assertEqual(photo_store.detect_format(png()), "png")
        self.assertEqual(photo_store.detect_format(webp()), "webp")
        self.assertIsNone(photo_store.detect_format(b"GIF89a" + bytes(100)))
        self.assertIsNone(photo_store.detect_format(b"RIFF\x00\x00\x00\x00WAVE" + bytes(100)))

    def test_uploaded_photo_compatible_with_mcp_reads(self):
        conn = db.open_database(self.db_path)
        try:
            result = photo_store.save_uploads(conn, [("сзади", "a.jpg", jpeg())], days_ago(1), "тест",
                                              photos_dir=self.photos)
            from fitness import records
            found = records.get_record(conn, "photo", result["ids"][0])
        finally:
            conn.close()
        self.assertEqual(found["view"], "back")
        self.assertIsNone(found["session_id"])
        self.assertTrue((self.photos / Path(*found["file_path"].split("/"))).is_file())

    def test_parse_multipart_rejects_garbage(self):
        for body, ctype in ((b"abc", "multipart/form-data; boundary=x"), (b"abc", "text/plain"),
                            (b"--x\r\nno-headers--x--", "multipart/form-data; boundary=x")):
            with self.subTest(body=body), self.assertRaises(app.BadRequest):
                app.parse_multipart(body, ctype)
        fields, files = app.parse_multipart(*multipart({"a": "б"}, [("file_front", "", b"")]))
        self.assertEqual((fields, files), ({"a": "б"}, []))
        data = jpeg(b"\r\n\r\n--")
        fields, files = app.parse_multipart(*multipart({}, [("file_front", "x.jpg", data)]))
        self.assertEqual(files, [("file_front", "x.jpg", data)])
