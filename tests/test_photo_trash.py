"""Корзина фотографий: перенос, восстановление, окончательное удаление, сбои на каждом шаге, пути, миграция 005.

Работает с временной базой и временными папками data/photos, data/photos_trash, data/backups.
Рабочие данные не используются. «Изображения» выдуманные.
"""

import re
import sqlite3
from pathlib import Path
from unittest import mock

from fitness import db, measurements, photo_trash, records
from fitness.errors import BackupError, ConfirmationError, NotFoundError, PhotoFileError, ValidationError
from fitness.web import readonly
from tests import health_helpers as h
from tests.test_dashboard import file_state
from tests.test_dashboard_photos import PhotoTestCase, days_ago, jpeg, png


class TrashTestCase(PhotoTestCase):
    def setUp(self):
        super().setUp()
        self.trash = self.db_path.parent / "photos_trash"
        self.backups = self.db_path.parent / "backups"

    def open(self):
        conn = db.open_database(self.db_path, create=False)
        self.addCleanup(conn.close)
        return conn

    def table(self, name):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY id")]
        finally:
            conn.close()

    def files(self, folder):
        if not folder.exists():
            return {}
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    def state(self):
        """Всё, что не должно пропасть: записи обеих таблиц и файлы обеих папок с содержимым."""
        return self.table("photos"), self.table("photo_trash"), self.files(self.photos), self.files(self.trash)

    def code(self, conn, pid):
        return records.prepare_delete(conn, "photo", pid)["confirmation_code"]

    def to_trash(self, conn, pid, **kwargs):
        return photo_trash.move_to_trash(conn, pid, self.code(conn, pid), photos_dir=self.photos,
                                         trash_dir=self.trash, backup_dir=self.backups, **kwargs)

    def restore(self, conn, tid):
        return photo_trash.restore(conn, tid, photos_dir=self.photos, trash_dir=self.trash)

    def purge(self, conn, tid, code=None):
        if code is None:
            code = photo_trash.prepare_purge(conn, tid, trash_dir=self.trash)["confirmation_code"]
        return photo_trash.purge(conn, tid, code, trash_dir=self.trash, backup_dir=self.backups)


class MoveToTrashTests(TrashTestCase):
    def test_web_delete_moves_file_and_hides_photo_from_gallery_and_compare(self):
        a = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"a"), notes="утро")
        b = self.add_existing("b.jpg", days_ago(1), "front", jpeg(b"b"))
        self.start()
        html = self.get_html(f"/photos/{a}/delete")
        code = re.search(r'name="code" value="([0-9A-F]+)"', html).group(1)
        status, headers, _ = self.form(f"/photos/{a}/delete", code=code, period="90")
        self.assertEqual(status, 303)

        self.assertEqual([r["id"] for r in self.rows()], [b])
        self.assertEqual(self.files(self.photos), {"b.jpg": jpeg(b"b")})
        self.assertEqual(self.files(self.trash), {"a.jpg": jpeg(b"a")})
        [item] = self.table("photo_trash")
        self.assertEqual((item["photo_id"], item["taken_on"], item["view"], item["notes"], item["file_path"],
                          item["trash_path"]), (a, days_ago(5), "front", "утро", "a.jpg", "a.jpg"))
        self.assertEqual(len(list(self.backups.glob("*before-delete-photo*.db"))), 1)

        gallery = self.get_html("/photos?period=90")
        self.assertEqual(self.sections(gallery)["Спереди"], [b])
        self.assertIn("Корзина (1)", gallery)
        self.assertIn("не найдена", self.get_html(f"/photos/compare?id={a}&id={b}"))
        self.assertEqual(self.request(f"/photo/{a}")[0], 404)
        self.assertEqual(self.request(f"/photos/{a}")[0], 404)

        trash = self.get_html("/photos/trash")
        self.assertIn(f'src="/photo-trash/{item["id"]}"', trash)
        for text in (days_ago(5), "Спереди", "утро", "Удалено:", "Восстановить", "Удалить навсегда"):
            self.assertIn(text, trash)
        status, hdrs, body = self.request(f"/photo-trash/{item['id']}")
        self.assertEqual((status, hdrs["Content-Type"], body), (200, "image/jpeg", jpeg(b"a")))

    def test_uploaded_photo_in_year_folder(self):
        self.start()
        self.upload([("file_back", "x.jpg", jpeg(b"x"))], taken_on=days_ago(2), notes="вечер")
        [row] = self.rows()
        conn = self.open()
        result = self.to_trash(conn, row["id"])
        self.assertEqual(result["trash_path"], row["file_path"])
        self.assertEqual(self.files(self.trash), {row["file_path"]: jpeg(b"x")})
        self.assertEqual(self.files(self.photos), {})
        self.assertIsNone(result["warning"])

    def test_wrong_or_stale_code_changes_nothing(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        conn = self.open()
        before = self.state()
        stale = self.code(conn, pid)
        for code in ("WRONG", "", None):
            with self.subTest(code=code), self.assertRaises(ConfirmationError):
                photo_trash.move_to_trash(conn, pid, code, photos_dir=self.photos, trash_dir=self.trash,
                                          backup_dir=self.backups)
        conn.execute("UPDATE photos SET notes = 'правка', updated_at = '2030-01-01T00:00:00Z' WHERE id = ?", (pid,))
        with self.assertRaises(ConfirmationError):
            photo_trash.move_to_trash(conn, pid, stale, photos_dir=self.photos, trash_dir=self.trash,
                                      backup_dir=self.backups)
        self.assertEqual(self.state()[1:], before[1:])
        self.assertFalse(self.backups.exists())

    def test_cancel_and_confirmation_pages_change_nothing(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        other = self.add_existing("b.jpg", days_ago(4), "back", jpeg(b"b"))
        conn.close()
        before, db_before = self.state(), file_state(self.db_path)
        self.start()
        delete_page = self.get_html(f"/photos/{other}/delete")
        self.assertIn(f'href="/photos/{other}?period=90">Отмена', delete_page)
        purge_page = self.get_html(f"/photos/trash/{tid}/purge")
        self.assertIn("Да, удалить навсегда", purge_page)
        self.assertIn('href="/photos/trash?period=90">Отмена', purge_page)
        for path in ("/photos/trash", f"/photos/trash/{tid}/purge", f"/photo-trash/{tid}", "/photos/trash/999/purge",
                     "/photo-trash/999", f"/photos/{other}/delete"):
            with self.subTest(path=path):
                self.assertIn(self.request(path)[0], (200, 404))
        # Отправка без кода или с чужим кодом — тоже отмена
        self.assertEqual(self.form(f"/photos/{other}/delete")[0], 400)
        self.assertEqual(self.form(f"/photos/trash/{tid}/purge", code="00000000")[0], 400)
        self.server.shutdown()
        self.assertEqual(self.state(), before)
        self.assertEqual(file_state(self.db_path), db_before)

    def test_trash_forms_need_token_and_origin(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        tid = self.to_trash(self.open(), pid)["trash_id"]
        before = self.state()
        self.start()
        self.assertEqual(self.form(f"/photos/trash/{tid}/restore", csrf="bad")[0], 403)
        self.assertEqual(self.post(f"/photos/trash/{tid}/restore", b"csrf=" + self.server.csrf_token.encode(),
                                   origin="http://evil.example")[0], 403)
        self.assertEqual(self.post(f"/photos/trash/{tid}/restore", b"csrf=" + self.server.csrf_token.encode(),
                                   origin=False)[0], 403)
        self.assertEqual(self.form("/photos/trash/999/restore")[0], 404)
        self.assertEqual(self.post("/photos/trash", b"x=1")[0], 405)
        self.assertEqual(self.state(), before)


class RestoreTests(TrashTestCase):
    def test_restore_keeps_all_metadata_and_number(self):
        conn = self.open()
        session = measurements.log_measurement(conn, {"measured_on": days_ago(5), "weight_kg": 80})["id"]
        conn.close()
        pid = self.add_existing("a.jpg", days_ago(5), "side", jpeg(b"a"), notes="после отпуска")
        conn = self.open()
        conn.execute("UPDATE photos SET session_id = ?, created_at = '2026-01-02T03:04:05Z',"
                     " updated_at = '2026-01-03T03:04:05Z' WHERE id = ?", (session, pid))
        [original] = self.table("photos")
        tid = self.to_trash(conn, pid)["trash_id"]
        conn.close()
        self.start()
        status, headers, _ = self.form(f"/photos/trash/{tid}/restore", period="90")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/photos/{pid}?period=90&ok=restored")
        self.assertEqual(self.table("photos"), [original])
        self.assertEqual(self.table("photo_trash"), [])
        self.assertEqual(self.files(self.photos), {"a.jpg": jpeg(b"a")})
        self.assertEqual(self.files(self.trash), {})
        self.assertIn("восстановлено", self.get_html(headers["Location"]))
        self.assertEqual(self.sections(self.get_html("/photos?period=90"))["Сбоку"], [pid])

    def test_restore_when_number_and_session_are_gone(self):
        conn = self.open()
        session = measurements.log_measurement(conn, {"measured_on": days_ago(5), "weight_kg": 80})["id"]
        conn.close()
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"a"), notes="н")
        conn = self.open()
        conn.execute("UPDATE photos SET session_id = ? WHERE id = ?", (session, pid))
        tid = self.to_trash(conn, pid)["trash_id"]
        conn.execute("DELETE FROM measurement_sessions WHERE id = ?", (session,))
        conn.close()
        newcomer = self.add_existing("b.jpg", days_ago(1), "back", jpeg(b"b"))
        self.assertEqual(newcomer, pid)                                   # номер занят новым фото
        result = self.restore(self.open(), tid)
        self.assertFalse(result["same_id"])
        row = next(r for r in self.table("photos") if r["id"] == result["photo_id"])
        self.assertEqual((row["taken_on"], row["view"], row["notes"], row["session_id"], row["file_path"]),
                         (days_ago(5), "front", "н", None, "a.jpg"))
        self.assertEqual(len(self.table("photos")), 2)

    def test_restore_does_not_overwrite_file_with_same_name(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"old"))
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        (self.photos / "a.jpg").write_bytes(jpeg(b"stranger"))           # чужой файл с тем же именем
        result = self.restore(conn, tid)
        self.assertEqual(result["file_path"], "a-2.jpg")
        self.assertEqual(self.files(self.photos), {"a.jpg": jpeg(b"stranger"), "a-2.jpg": jpeg(b"old")})
        self.assertEqual(self.table("photos")[0]["file_path"], "a-2.jpg")

    def test_restore_refuses_duplicate_of_active_photo(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"same"))
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        conn.close()
        self.add_existing("again.jpg", days_ago(1), "front", jpeg(b"same"))
        before = self.state()
        with self.assertRaises(ValidationError) as ctx:
            self.restore(self.open(), tid)
        self.assertIn("уже есть в галерее", str(ctx.exception))
        self.assertEqual(self.state(), before)

    def test_restore_with_missing_trash_file_stops(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        (self.trash / "a.jpg").unlink()
        before = self.state()
        with self.assertRaises(PhotoFileError) as ctx:
            self.restore(conn, tid)
        self.assertIn("не найден в корзине", str(ctx.exception))
        self.assertEqual(self.state(), before)
        conn.close()
        self.start()
        page = self.get_html("/photos/trash")
        self.assertIn("Файл в корзине не найден", page)
        self.assertNotIn("Восстановить</button>", page)
        status, _, html = self.form(f"/photos/trash/{tid}/restore")
        self.assertEqual(status, 400)
        self.assertIn("не найден в корзине", html)
        self.assertEqual(self.state(), before)


class PurgeTests(TrashTestCase):
    def test_purge_removes_record_and_file_after_backup(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"a"))
        keep = self.add_existing("b.jpg", days_ago(5), "back", jpeg(b"b"))
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        conn.close()
        self.start()
        page = self.get_html(f"/photos/trash/{tid}/purge")
        code = re.search(r'name="code" value="([0-9A-F]+)"', page).group(1)
        self.assertEqual(self.form(f"/photos/trash/{tid}/purge", code="WRONG")[0], 400)
        self.assertEqual(len(self.table("photo_trash")), 1)
        status, headers, _ = self.form(f"/photos/trash/{tid}/purge", code=code, period="90")
        self.assertEqual(status, 303)
        self.assertEqual(self.table("photo_trash"), [])
        self.assertEqual(self.files(self.trash), {})
        self.assertEqual(self.files(self.photos), {"b.jpg": jpeg(b"b")})
        self.assertEqual([r["id"] for r in self.rows()], [keep])
        self.assertEqual(len(list(self.backups.glob("*before-purge-photo*.db"))), 1)
        self.assertIn("удалено навсегда", self.get_html(headers["Location"]))
        self.assertEqual(self.request(f"/photos/trash/{tid}/purge")[0], 404)

    def test_purge_of_record_without_file_needs_its_own_confirmation(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg())
        conn = self.open()
        tid = self.to_trash(conn, pid)["trash_id"]
        code_with_file = photo_trash.prepare_purge(conn, tid, trash_dir=self.trash)["confirmation_code"]
        (self.trash / "a.jpg").unlink()
        with self.assertRaises(ConfirmationError):                       # состояние изменилось после показа
            self.purge(conn, tid, code_with_file)
        self.assertEqual(len(self.table("photo_trash")), 1)
        prepared = photo_trash.prepare_purge(conn, tid, trash_dir=self.trash)
        self.assertFalse(prepared["file_present"])
        self.purge(conn, tid, prepared["confirmation_code"])
        self.assertEqual(self.table("photo_trash"), [])

    def test_purge_never_touches_files_outside_trash(self):
        outside = self.db_path.parent / "keep.jpg"
        outside.write_bytes(jpeg(b"keep"))
        self.photos.mkdir(parents=True, exist_ok=True)
        (self.photos / "victim.jpg").write_bytes(jpeg(b"victim"))
        conn = self.open()
        for bad in ("../keep.jpg", "../photos/victim.jpg", str(outside), "..\\keep.jpg"):
            tid = conn.execute(
                "INSERT INTO photo_trash (photo_id, taken_on, file_path, trash_path, created_at, updated_at)"
                " VALUES (1, ?, 'x.jpg', ?, 'c', 'u')", (days_ago(1), bad)).lastrowid
            with self.subTest(bad=bad):
                with self.assertRaises(PhotoFileError):
                    self.purge(conn, tid, "ANYCODE")
                with self.assertRaises(PhotoFileError):
                    self.restore(conn, tid)
                self.assertIsNone(photo_trash.trash_file(photo_trash.get_trash_item(conn, tid), self.trash))
        conn.close()
        self.assertEqual(outside.read_bytes(), jpeg(b"keep"))
        self.assertEqual(self.files(self.photos), {"victim.jpg": jpeg(b"victim")})
        self.start()
        for tid in (1, 2, 3, 4):
            self.assertEqual(self.request(f"/photo-trash/{tid}")[0], 404)
        self.assertIn("Файл в корзине не найден", self.get_html("/photos/trash"))


class FailureTests(TrashTestCase):
    """Сбой на любом шаге: файл не теряется, запись не ссылается на отсутствующий файл, метаданные целы."""

    def setUp(self):
        super().setUp()
        self.pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"a"), notes="важно")

    def assert_photo_active(self):
        [row] = self.table("photos")
        self.assertEqual((row["id"], row["file_path"], row["notes"]), (self.pid, "a.jpg", "важно"))
        self.assertEqual(self.files(self.photos), {"a.jpg": jpeg(b"a")})
        self.assertEqual((self.table("photo_trash"), self.files(self.trash)), ([], {}))

    def test_backup_failure(self):
        conn = self.open()
        with mock.patch.object(photo_trash, "backup", side_effect=OSError("диск заполнен")):
            with self.assertRaises(BackupError) as ctx:
                self.to_trash(conn, self.pid)
        self.assertIn("диск заполнен", str(ctx.exception))
        self.assert_photo_active()

    def test_backup_check_failure(self):
        conn = self.open()
        with mock.patch.object(photo_trash, "verify_backup_file", side_effect=BackupError("копия повреждена")):
            with self.assertRaises(BackupError):
                self.to_trash(conn, self.pid)
        self.assert_photo_active()

    def test_backup_failure_in_web_shows_error(self):
        self.start()
        code = re.search(r'name="code" value="([0-9A-F]+)"', self.get_html(f"/photos/{self.pid}/delete")).group(1)
        with mock.patch.object(photo_trash, "backup", side_effect=OSError("нет места")):
            status, _, html = self.form(f"/photos/{self.pid}/delete", code=code)
        self.assertEqual(status, 400)
        self.assertIn("Резервная копия не создана", html)
        self.assert_photo_active()

    def test_file_move_failure(self):
        conn = self.open()
        with mock.patch.object(photo_trash, "_place_copy", side_effect=PermissionError("файл занят")):
            with self.assertRaises(PhotoFileError) as ctx:
                self.to_trash(conn, self.pid)
        self.assertIn("файл занят", str(ctx.exception))
        self.assert_photo_active()

    def test_copy_mismatch_is_rolled_back(self):
        def broken_copy(src, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"truncated")
        conn = self.open()
        with mock.patch.object(photo_trash, "_place_copy", side_effect=broken_copy):
            with self.assertRaises(PhotoFileError):
                self.to_trash(conn, self.pid)
        self.assert_photo_active()

    def test_without_hard_links_a_copy_is_used(self):
        conn = self.open()
        with mock.patch.object(photo_trash.os, "link", side_effect=OSError("не поддерживается")):
            self.to_trash(conn, self.pid)
        self.assertEqual(self.files(self.trash), {"a.jpg": jpeg(b"a")})
        self.assertEqual((self.rows(), self.files(self.photos)), ([], {}))

    def test_database_failure_on_delete(self):
        conn = self.open()
        conn.execute("CREATE TRIGGER fail_trash BEFORE INSERT ON photo_trash BEGIN SELECT RAISE(ABORT, 'сбой'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.to_trash(conn, self.pid)
        self.assert_photo_active()
        conn.execute("DROP TRIGGER fail_trash")
        conn.execute("CREATE TRIGGER fail_photos BEFORE DELETE ON photos BEGIN SELECT RAISE(ABORT, 'сбой'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.to_trash(conn, self.pid)
        self.assert_photo_active()

    def test_database_failure_in_web_reports_nothing_changed(self):
        conn = self.open()
        conn.execute("CREATE TRIGGER fail_trash BEFORE INSERT ON photo_trash BEGIN SELECT RAISE(ABORT, 'сбой'); END")
        conn.close()
        self.start()
        code = re.search(r'name="code" value="([0-9A-F]+)"', self.get_html(f"/photos/{self.pid}/delete")).group(1)
        status, _, html = self.form(f"/photos/{self.pid}/delete", code=code)
        self.assertEqual(status, 500)
        self.assertIn("Ничего не сохранено", html)
        self.assert_photo_active()

    def test_original_not_removable_leaves_extra_copy_only(self):
        conn = self.open()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("файл открыт")):
            result = self.to_trash(conn, self.pid)
        self.assertIn("лишняя копия", result["warning"])
        self.assertEqual(self.rows(), [])
        [item] = self.table("photo_trash")
        self.assertEqual((item["notes"], item["trash_path"]), ("важно", "a.jpg"))
        self.assertEqual(self.files(self.trash), {"a.jpg": jpeg(b"a")})    # запись корзины -> существующий файл

    def test_database_failure_on_restore(self):
        conn = self.open()
        tid = self.to_trash(conn, self.pid)["trash_id"]
        before = self.state()
        conn.execute("CREATE TRIGGER fail_photos BEFORE INSERT ON photos BEGIN SELECT RAISE(ABORT, 'сбой'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.restore(conn, tid)
        self.assertEqual(self.state(), before)

    def test_file_failure_on_restore(self):
        conn = self.open()
        tid = self.to_trash(conn, self.pid)["trash_id"]
        before = self.state()
        with mock.patch.object(photo_trash, "_place_copy", side_effect=PermissionError("нет доступа")):
            with self.assertRaises(PhotoFileError):
                self.restore(conn, tid)
        self.assertEqual(self.state(), before)

    def test_failures_on_purge(self):
        conn = self.open()
        tid = self.to_trash(conn, self.pid)["trash_id"]
        before = self.state()
        with mock.patch.object(photo_trash, "backup", side_effect=sqlite3.OperationalError("заблокировано")):
            with self.assertRaises(BackupError):
                self.purge(conn, tid)
        self.assertEqual(self.state(), before)
        conn.execute("CREATE TRIGGER fail_purge BEFORE DELETE ON photo_trash BEGIN SELECT RAISE(ABORT, 'сбой'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.purge(conn, tid)
        self.assertEqual(self.state(), before)                             # файл вернулся под прежним именем
        conn.execute("DROP TRIGGER fail_purge")
        with mock.patch.object(photo_trash.os, "rename", side_effect=PermissionError("файл открыт")):
            with self.assertRaises(PhotoFileError):
                self.purge(conn, tid)
        self.assertEqual(self.state(), before)

    def test_missing_file_stops_delete(self):
        (self.photos / "a.jpg").unlink()
        before = self.state()
        self.start()
        code = re.search(r'name="code" value="([0-9A-F]+)"', self.get_html(f"/photos/{self.pid}/delete")).group(1)
        status, _, html = self.form(f"/photos/{self.pid}/delete", code=code)
        self.assertEqual(status, 400)
        self.assertIn("не найден", html)
        self.assertEqual(self.state(), before)                             # запись не удалена автоматически
        self.assertFalse(self.backups.exists())


class NamesAndPathsTests(TrashTestCase):
    def test_same_name_in_trash_is_not_overwritten(self):
        self.trash.mkdir(parents=True)
        (self.trash / "a.jpg").write_bytes(jpeg(b"stranger"))
        first = self.add_existing("a.jpg", days_ago(5), "front", jpeg(b"1"))
        conn = self.open()
        self.assertEqual(self.to_trash(conn, first)["trash_path"], "a-2.jpg")
        conn.close()
        second = self.add_existing("a.jpg", days_ago(4), "front", jpeg(b"2"))
        self.assertEqual(self.to_trash(self.open(), second)["trash_path"], "a-3.jpg")
        self.assertEqual(self.files(self.trash), {"a.jpg": jpeg(b"stranger"), "a-2.jpg": jpeg(b"1"),
                                                  "a-3.jpg": jpeg(b"2")})

    def test_paths_outside_folders_are_refused(self):
        base = self.tmp / "base"
        for bad in ("../x.jpg", "..\\x.jpg", "a/../../x.jpg", "/etc/x.jpg", "\\x.jpg", "C:\\x.jpg", "C:x.jpg",
                    "\\\\server\\share\\x.jpg", "//server/share/x.jpg", "x.db", "x", "", "   ", "a\x00.jpg", None):
            with self.subTest(bad=bad), self.assertRaises(PhotoFileError):
                photo_trash.safe_path(base, bad, "папки")
        self.assertEqual(photo_trash.safe_path(base, "2026/a.jpg", "папки"), (base / "2026" / "a.jpg").resolve())

    def test_photo_row_pointing_outside_is_not_moved(self):
        outside = self.db_path.parent / "secret.jpg"
        outside.write_bytes(jpeg(b"s"))
        conn = self.open()
        pid = conn.execute("INSERT INTO photos (taken_on, file_path, view) VALUES (?, '../secret.jpg', 'front')",
                           (days_ago(1),)).lastrowid
        before = self.state()
        with self.assertRaises(PhotoFileError):
            self.to_trash(conn, pid)
        self.assertEqual(self.state(), before)
        self.assertEqual(outside.read_bytes(), jpeg(b"s"))
        self.assertFalse(self.backups.exists())

    def test_not_found(self):
        conn = self.open()
        for call in (lambda: self.restore(conn, 5), lambda: self.purge(conn, 5, "X"),
                     lambda: photo_trash.move_to_trash(conn, 5, "X", self.photos, self.trash, self.backups)):
            with self.assertRaises(NotFoundError):
                call()


class ReadonlyLayerTests(TrashTestCase):
    def test_analytics_layer_stays_readonly(self):
        source = Path(readonly.__file__).read_text(encoding="utf-8")
        self.assertNotIn("photo_trash", source)
        self.assertNotIn("photo_store", source)
        conn = readonly.connect_readonly(self.db_path)
        try:
            for sql in ("INSERT INTO photo_trash (photo_id, taken_on, file_path, trash_path, created_at, updated_at)"
                        " VALUES (1, '2026-01-01', 'a.jpg', 'a.jpg', 'c', 'u')", "DELETE FROM photo_trash",
                        "DELETE FROM photos"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_trash_pages_render_through_readonly_connection(self):
        pid = self.add_existing("a.jpg", days_ago(5), "front", jpeg(), notes="<b>x</b>")
        tid = self.to_trash(self.open(), pid)["trash_id"]
        self.start()
        with mock.patch("fitness.web.app.readonly_connection", wraps=readonly.readonly_connection) as spy:
            for path in ("/photos/trash", f"/photos/trash/{tid}/purge", f"/photo-trash/{tid}"):
                self.assertEqual(self.request(path)[0], 200, path)
        self.assertEqual(spy.call_count, 3)
        page = self.get_html("/photos/trash")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("<script", page)

    def test_mcp_delete_tool_is_unchanged(self):
        # Удаление через MCP (records.delete_record) по-прежнему удаляет только запись, корзину не трогает.
        pid = self.add_existing("a.jpg", days_ago(5), "front", png())
        conn = self.open()
        records.delete_record(conn, "photo", pid, self.code(conn, pid), backup_dir=self.backups)
        self.assertEqual((self.rows(), self.table("photo_trash")), ([], []))
        self.assertTrue((self.photos / "a.jpg").is_file())


class Migration005Tests(h.IsolatedTestCase):
    def at_version_4(self, path):
        h.make_v2_database(path, self.tmp, health_rows=True)
        conn = db.connect(path, create=False)
        photos = self.tmp / "photos"
        photos.mkdir(exist_ok=True)
        (photos / "a.jpg").write_bytes(jpeg())
        measurements.add_photo(conn, {"taken_on": "2026-01-05", "file_path": "a.jpg", "view": "back", "notes": "н"},
                               photos_dir=photos)
        conn.close()
        pinned = h.migrations_dir(self.tmp, f"v4-only-{path.stem}", with_002=True, with_003=True, with_004=True)
        with mock.patch.object(db, "MIGRATIONS_DIR", pinned):
            assert db.upgrade_database(path)["to_version"] == 4

    def test_v4_gets_trash_table_and_keeps_photos(self):
        path = self.tmp / "data" / "v4.db"
        self.at_version_4(path)
        conn = db.connect(path, create=False)
        before = h.snapshot(conn)
        conn.close()
        report = db.upgrade_database(path)
        self.assertEqual((report["from_version"], report["to_version"], report["applied"]), (4, 5, [5]))
        self.assertIn("before-migration-005", report["backup"])
        conn = db.connect(path, create=False)
        try:
            after = h.snapshot(conn)
            self.assertEqual(after.pop("photo_trash"), [])
            self.assertEqual(after, before)
            self.assertEqual(len(before["photos"]), 1)
        finally:
            conn.close()

    def test_fresh_and_upgraded_databases_have_the_same_schema(self):
        old, fresh = self.tmp / "data" / "old.db", self.tmp / "data" / "fresh.db"
        self.at_version_4(old)
        db.upgrade_database(old)
        db.open_database(fresh).close()
        schemas = []
        for path in (old, fresh):
            conn = db.connect(path, create=False)
            try:
                self.assertEqual(db.schema_version(conn), 5)
                schemas.append(sorted(r[0] for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name LIKE '%photo_trash%' AND sql IS NOT NULL")))
            finally:
                conn.close()
        self.assertEqual(schemas[0], schemas[1])
        self.assertEqual(len(schemas[0]), 2)

    def test_constraints(self):
        conn = db.open_database(self.tmp / "data" / "c.db")
        insert = ("INSERT INTO photo_trash (photo_id, taken_on, file_path, trash_path, view, created_at, updated_at)"
                  " VALUES (?, ?, ?, ?, ?, 'c', 'u')")
        try:
            conn.execute(insert, (1, "2026-01-01", "a.jpg", "a.jpg", "front"))
            for label, values in (("дубль пути корзины", (2, "2026-01-01", "b.jpg", "a.jpg", None)),
                                  ("плохая дата", (2, "01.01.2026", "b.jpg", "b.jpg", None)),
                                  ("плохой ракурс", (2, "2026-01-01", "b.jpg", "b.jpg", "top")),
                                  ("пустой путь", (2, "2026-01-01", "", "c.jpg", None)),
                                  ("номер 0", (0, "2026-01-01", "b.jpg", "d.jpg", None))):
                with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(insert, values)
        finally:
            conn.close()
