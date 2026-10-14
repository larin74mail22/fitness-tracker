"""Веб-панель: только чтение, только loopback, корректные страницы.

Каждый тест поднимает панель на случайном свободном порту 127.0.0.1 с временной базой
и останавливает её после проверки. Рабочая data/fitness.db не используется. Данные выдуманные.
"""

import hashlib
import http.client
import inspect
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from fitness import alcohol, db, measurements, notes, nutrition, workouts
from fitness.web import app
from fitness.web.readonly import DatabaseUnavailable, connect_readonly

PAGES = ["/", "/measurements", "/measurements?type=waist", "/workouts", "/workouts?exercise=%D0%96%D0%B8%D0%BC",
         "/nutrition", "/alcohol", "/photos", "/?period=all", "/nutrition?period=30", "/alcohol?period=bogus"]


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def file_state(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_size


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fitness-web-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / "data" / "fitness.db"
        self.server = None
        quiet = mock.patch.object(app, "log")   # журнал запросов не засоряет вывод тестов
        quiet.start()
        self.addCleanup(quiet.stop)

    def fill(self):
        conn = db.open_database(self.db_path)
        try:
            measurements.log_measurement(conn, {"measured_on": days_ago(20), "weight_kg": 82.0,
                                                "measurements": [{"type": "waist", "value": 88}]})
            session = measurements.log_measurement(conn, {"measured_on": days_ago(5), "weight_kg": 81.2,
                                                          "measurements": [{"type": "waist", "value": 86.5}]})
            workouts.log_workout(conn, {"workout_date": days_ago(3), "title": "<script>alert(1)</script>",
                                        "workout_type": "strength", "exercises": [
                                            {"name": "Жим", "discomfort": "плечо", "sets": [
                                                {"weight_kg": 40, "reps": 10, "is_warmup": True},
                                                {"weight_kg": 60, "reps": 8, "rir": 2}]}]})
            nutrition.log_meal(conn, {"eaten_on": days_ago(2), "meal_type": "breakfast", "items": [
                {"name": "Овсянка", "quantity": 60, "unit": "g", "kcal": 220, "protein_g": 8,
                 "nutrition_source": "label", "is_estimate": False},
                {"name": "Яблоко"}]})
            alcohol.log_alcohol(conn, {"consumed_on": days_ago(1), "drink": "Пиво", "volume_ml": 500,
                                       "abv_percent": 5})
            alcohol.log_alcohol(conn, {"consumed_on": days_ago(1), "drink": "Вино"})
            notes.add_note(conn, {"note_date": days_ago(1), "category": "тест", "text": "заметка"})
            photos = self.db_path.parent / "photos"
            photos.mkdir()
            (photos / "front.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
            measurements.add_photo(conn, {"taken_on": days_ago(5), "file_path": "front.jpg",
                                          "session_id": session["id"], "view": "front"}, photos_dir=photos)
        finally:
            conn.close()   # закрытие последнего соединения переносит WAL в основной файл

    def start(self):
        self.server = app.make_server(port=0, db_path=self.db_path)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        return self.server

    def request(self, path, method="GET", host=None):
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {"Host": host} if host else {}
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def get_html(self, path):
        status, headers, body = self.request(path)
        self.assertEqual(status, 200, path)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        return body.decode("utf-8")


class LoopbackTests(DashboardTestCase):
    def test_bound_to_loopback_only(self):
        server = self.start()
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertEqual(app.HOST, "127.0.0.1")

    def test_host_cannot_be_changed(self):
        self.assertNotIn("host", inspect.signature(app.make_server).parameters)
        self.assertNotIn("--host", inspect.getsource(app.main))

    def test_not_reachable_from_other_address(self):
        self.start()
        port = self.server.server_address[1]
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            addresses = set()
        external = [a for a in addresses if not a.startswith("127.")]
        if not external:
            self.skipTest("у компьютера нет сетевого адреса, кроме 127.0.0.1")
        for address in external:
            with self.subTest(address=address), self.assertRaises(OSError):
                socket.create_connection((address, port), timeout=2).close()

    def test_foreign_host_header_refused(self):
        self.fill()
        self.start()
        status, _, _ = self.request("/", host="evil.example.com")
        self.assertEqual(status, 400)
        status, _, _ = self.request("/", host=f"localhost:{self.server.server_address[1]}")
        self.assertEqual(status, 200)

    def test_write_methods_refused(self):
        self.fill()
        self.start()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, headers, _ = self.request("/", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["Allow"], "GET, HEAD")


class ReadOnlyTests(DashboardTestCase):
    def test_pages_do_not_change_database(self):
        self.fill()
        before = file_state(self.db_path)
        self.start()
        for path in PAGES + ["/photo/1", "/photo/999", "/nope"]:
            self.request(path)
            self.request(path, method="HEAD")
        self.request("/", method="POST")
        self.server.shutdown()
        self.assertEqual(file_state(self.db_path), before)
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_connection_refuses_writes(self):
        self.fill()
        conn = connect_readonly(self.db_path)
        try:
            for sql in ("INSERT INTO notes (note_date, category, text) VALUES ('2025-01-01', 'x', 'y')",
                        "DELETE FROM workouts", "UPDATE meals SET notes = 'x'", "CREATE TABLE t (x)",
                        "DROP TABLE notes", "PRAGMA journal_mode = DELETE", "ATTACH DATABASE ':memory:' AS m"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 1)
        finally:
            conn.close()

    def test_missing_database_not_created(self):
        self.start()
        html = self.get_html("/")
        self.assertIn("База ещё не создана", html)
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.db_path.parent.exists())

    def test_wrong_schema_not_migrated(self):
        self.db_path.parent.mkdir(parents=True)
        raw = sqlite3.connect(self.db_path)
        raw.execute("CREATE TABLE something (x)")
        raw.close()
        before = file_state(self.db_path)
        with self.assertRaises(DatabaseUnavailable):
            connect_readonly(self.db_path)
        self.start()
        self.assertIn("Версия схемы базы 0", self.get_html("/workouts"))
        self.assertEqual(file_state(self.db_path), before)

    def test_corrupt_database_reported(self):
        self.db_path.parent.mkdir(parents=True)
        self.db_path.write_bytes(b"not a database" * 300)
        self.start()
        self.assertIn("База недоступна", self.get_html("/"))


class PageTests(DashboardTestCase):
    def test_empty_database_states(self):
        db.open_database(self.db_path).close()
        self.start()
        self.assertIn("База пуста", self.get_html("/"))
        for path in ("/measurements", "/workouts", "/nutrition", "/alcohol", "/photos"):
            with self.subTest(path=path):
                html = self.get_html(path)
                self.assertTrue("Пока нет записей" in html or "Фото за этот период нет" in html)

    def test_pages_with_data(self):
        self.fill()
        self.start()
        overview = self.get_html("/")
        self.assertIn("81.2 кг", overview)
        self.assertIn("<svg", overview)

        waist = self.get_html("/measurements?type=waist")
        self.assertIn("Талия", waist)
        self.assertIn("86.5", waist)

        work = self.get_html("/workouts")
        self.assertNotIn("<script>alert(1)</script>", work)          # HTML экранируется
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", work)
        self.assertIn("Дискомфорт: плечо", work)
        self.assertIn("RIR 2", work)
        bench = self.get_html("/workouts?exercise=%D0%96%D0%B8%D0%BC")
        self.assertIn("лучший рабочий вес", bench)
        self.assertIn("60 кг", bench)

        food = self.get_html("/nutrition")
        self.assertIn("Овсянка", food)
        self.assertIn("без калорий: 1", food)

        drinks = self.get_html("/alcohol")
        self.assertIn("19.7 г", drinks)
        self.assertIn("Без объёма или крепости: 1", drinks)

        photos = self.get_html("/photos")
        self.assertIn('src="/photo/1"', photos)

    def test_photo_served_and_checked(self):
        self.fill()
        self.start()
        status, headers, body = self.request("/photo/1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(body, b"\xff\xd8\xff fake jpeg")
        self.assertEqual(self.request("/photo/999")[0], 404)
        self.assertEqual(self.request("/photo/../../etc")[0], 404)

    def test_security_headers_and_404(self):
        self.fill()
        self.start()
        status, headers, _ = self.request("/")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("/admin")[0], 404)

    def test_no_external_resources(self):
        self.fill()
        self.start()
        for path in PAGES:
            html = self.get_html(path)
            self.assertNotIn("http://", html.replace("http://127.0.0.1", ""), path)
            self.assertNotIn("https://", html, path)
            self.assertNotIn("<script", html, path)


if __name__ == "__main__":
    unittest.main()
