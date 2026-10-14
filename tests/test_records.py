"""Поиск, удаление с подтверждением, защита от SQL-инъекций."""

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from fitness import alcohol, db, measurements, notes, nutrition, records, workouts
from fitness.errors import BackupError, ConfirmationError, NotFoundError, ValidationError
from tests.helpers import TempDbTestCase


class SearchTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        measurements.log_measurement(self.conn, {"measured_on": "2025-01-05", "weight_kg": 80,
                                                 "notes": "после отпуска"})
        workouts.log_workout(self.conn, {"workout_date": "2025-01-06", "title": "Ноги", "exercises": [
            {"name": "Присед", "sets": [{"weight_kg": 90, "reps": 5}], "discomfort": "Колено ныло"}]})
        nutrition.log_meal(self.conn, {"eaten_on": "2025-01-06", "items": [{"name": "Гречка с курицей"}]})
        alcohol.log_alcohol(self.conn, {"consumed_on": "2025-02-01", "drink": "Красное вино"})
        notes.add_note(self.conn, {"note_date": "2025-02-02", "category": "травма", "text": "Болит колено"})

    def test_keyword_search_is_case_insensitive_for_cyrillic(self):
        res = records.search_records(self.conn, query="КОЛЕНО")
        found = {(r["record_type"], r["date"]) for r in res["results"]}
        self.assertEqual(found, {("workout", "2025-01-06"), ("note", "2025-02-02")})

    def test_period_and_type_filters(self):
        res = records.search_records(self.conn, date_from="2025-01-01", date_to="2025-01-31")
        self.assertEqual(res["count"], 3)
        res = records.search_records(self.conn, record_types=["meal"], query="гречка")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["summary"], "Гречка с курицей")

    def test_note_category(self):
        res = records.search_records(self.conn, note_category="Травма")
        self.assertEqual([r["record_type"] for r in res["results"]], ["note"])

    def test_requires_filter(self):
        with self.assertRaises(ValidationError):
            records.search_records(self.conn)

    def test_like_wildcards_are_literal(self):
        self.assertEqual(records.search_records(self.conn, query="%")["count"], 0)
        self.assertEqual(records.search_records(self.conn, query="_")["count"], 0)

    def test_sql_injection_is_plain_text(self):
        res = records.search_records(self.conn, query="'; DROP TABLE notes; --")
        self.assertEqual(res["count"], 0)
        self.assertEqual(self.count("notes"), 1)
        with self.assertRaises(ValidationError):
            records.search_records(self.conn, record_types=["notes; DROP TABLE notes"])

    def test_limit(self):
        res = records.search_records(self.conn, date_from="2025-01-01", date_to="2025-12-31", limit=2)
        self.assertEqual(res["count"], 2)
        self.assertTrue(res["truncated"])


class DeleteTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.workout = workouts.log_workout(self.conn, {"workout_date": "2025-01-06", "title": "Ноги", "exercises": [
            {"name": "Присед", "sets": [{"weight_kg": 90, "reps": 5}, {"weight_kg": 90, "reps": 5}]}]})
        self.backups = self.tmp / "backups"

    def test_two_step_delete_with_backup(self):
        preview = records.prepare_delete(self.conn, "workout", self.workout["id"])
        self.assertEqual(preview["record"]["title"], "Ноги")
        self.assertEqual(self.count("workouts"), 1)          # предпросмотр ничего не удаляет
        res = records.delete_record(self.conn, "workout", self.workout["id"], preview["confirmation_code"],
                                    backup_dir=self.backups)
        self.assertTrue(res["deleted"])
        for table in ("workouts", "workout_exercises", "exercise_sets"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(len(list(self.backups.glob("*.db"))), 1)

    def test_wrong_code_refused(self):
        with self.assertRaises(ConfirmationError):
            records.delete_record(self.conn, "workout", self.workout["id"], "WRONG", backup_dir=self.backups)
        with self.assertRaises(ConfirmationError):
            records.delete_record(self.conn, "workout", self.workout["id"], None, backup_dir=self.backups)
        self.assertEqual(self.count("workouts"), 1)
        self.assertFalse(self.backups.exists())

    def test_code_expires_after_change(self):
        preview = records.prepare_delete(self.conn, "workout", self.workout["id"])
        self.conn.execute("UPDATE workouts SET notes = 'изменено', updated_at = '2099-01-01T00:00:00Z'")
        with self.assertRaises(ConfirmationError):
            records.delete_record(self.conn, "workout", self.workout["id"], preview["confirmation_code"],
                                  backup_dir=self.backups)

    def test_missing_record(self):
        with self.assertRaises(NotFoundError):
            records.prepare_delete(self.conn, "meal", 12345)
        with self.assertRaises(ValidationError):
            records.prepare_delete(self.conn, "users", 1)


class DeleteCascadeTests(TempDbTestCase):
    """Что удаляется вместе с записью: правила ON DELETE из 001_initial.sql и текущий delete_record.

    Только временная база и временная папка фото. Код удаления файлы фото не трогает,
    поэтому тесты проверяют, что файл остаётся на диске.
    """

    def setUp(self):
        super().setUp()
        self.backups = self.tmp / "backups"
        self.photos = self.tmp / "photos"
        assert self.photos.resolve() != db.PHOTOS_DIR.resolve()
        self.photos.mkdir()

    def _fake_photo(self, name):
        path = self.photos / name
        path.write_bytes(b"fake image")
        return path

    def _delete(self, record_type, record_id):
        preview = records.prepare_delete(self.conn, record_type, record_id)
        res = records.delete_record(self.conn, record_type, record_id, preview["confirmation_code"],
                                    backup_dir=self.backups)
        self.assertTrue(res["deleted"])
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        return res

    def test_delete_measurement_cascades_and_unlinks_photo(self):
        session = measurements.log_measurement(self.conn, {"measured_on": "2025-03-01", "weight_kg": 80, "measurements": [
            {"type": "waist", "value": 85}, {"type": "chest", "value": 100}]})
        other = measurements.log_measurement(self.conn, {"measured_on": "2025-03-08", "weight_kg": 79, "measurements": [
            {"type": "waist", "value": 84}]})
        file = self._fake_photo("front.jpg")
        photo = measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "front.jpg",
                                                   "session_id": session["id"]}, photos_dir=self.photos)
        types_before = self.count("measurement_types")

        self._delete("measurement", session["id"])

        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM measurement_sessions")], [other["id"]])
        # замеры удалённой сессии удалены (CASCADE), замер другой сессии на месте
        rows = self.conn.execute("SELECT session_id, type_code FROM body_measurements").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(other["id"], "waist")])
        # строка фото остаётся и только теряет привязку к замеру (SET NULL), файл на месте
        row = self.conn.execute("SELECT session_id, file_path FROM photos WHERE id = ?", (photo["id"],)).fetchone()
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["file_path"], "front.jpg")
        self.assertTrue(file.is_file())
        # справочник видов замеров не затронут
        self.assertEqual(self.count("measurement_types"), types_before)

    def test_delete_meal_removes_items(self):
        meal = nutrition.log_meal(self.conn, {"eaten_on": "2025-03-01", "items": [
            {"name": "Овсянка", "quantity": 60, "unit": "g"}, {"name": "Банан", "quantity": 1, "unit": "pcs"}]})
        other = nutrition.log_meal(self.conn, {"eaten_on": "2025-03-02", "items": [{"name": "Творог"}]})
        self.assertEqual(self.count("meal_items"), 3)

        self._delete("meal", meal["id"])

        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM meals")], [other["id"]])
        rows = self.conn.execute("SELECT meal_id, name FROM meal_items").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(other["id"], "Творог")])

    def test_delete_note_removes_fields(self):
        note = notes.add_note(self.conn, {"note_date": "2025-03-01", "category": "сон", "text": "Плохо спал",
                                          "fields": [{"key": "часы", "value": 5, "unit": "h"},
                                                     {"key": "причина", "value": "шум"}]})
        other = notes.add_note(self.conn, {"note_date": "2025-03-02", "category": "сон", "text": "Выспался",
                                           "fields": [{"key": "часы", "value": 8, "unit": "h"}]})
        self.assertEqual(self.count("note_fields"), 3)

        self._delete("note", note["id"])

        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM notes")], [other["id"]])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT note_id FROM note_fields")], [other["id"]])

    def test_delete_photo_removes_row_but_keeps_file(self):
        session = measurements.log_measurement(self.conn, {"measured_on": "2025-03-01", "weight_kg": 80, "measurements": [
            {"type": "waist", "value": 85}]})
        file = self._fake_photo("side.jpg")
        kept = self._fake_photo("back.jpg")
        photo = measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "side.jpg",
                                                   "session_id": session["id"]}, photos_dir=self.photos)
        measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "back.jpg",
                                           "session_id": session["id"]}, photos_dir=self.photos)

        res = self._delete("photo", photo["id"])

        # удалена только строка этого фото; второе фото, замер и его обхваты на месте
        self.assertEqual([r[0] for r in self.conn.execute("SELECT file_path FROM photos")], ["back.jpg"])
        self.assertEqual(self.count("measurement_sessions"), 1)
        self.assertEqual(self.count("body_measurements"), 1)
        # файлы на диске код удаления не трогает
        self.assertTrue(file.is_file())
        self.assertTrue(kept.is_file())
        # сейчас ответ никак не сообщает, что файл остался (неоднозначность, зафиксирована в отчёте)
        self.assertEqual(set(res), {"deleted", "record_type", "id", "backup"})

    def test_deleted_photo_file_can_be_registered_again(self):
        self._fake_photo("front.jpg")
        photo = measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "front.jpg"},
                                       photos_dir=self.photos)
        self._delete("photo", photo["id"])
        measurements.add_photo(self.conn, {"taken_on": "2025-03-01", "file_path": "front.jpg"},
                               photos_dir=self.photos)
        self.assertEqual(self.count("photos"), 1)

    def test_backup_holds_deleted_record_and_children(self):
        meal = nutrition.log_meal(self.conn, {"eaten_on": "2025-03-01", "items": [{"name": "Суп"}, {"name": "Хлеб"}]})
        res = self._delete("meal", meal["id"])
        backups = list(self.backups.glob("*.db"))
        self.assertEqual(backups, [Path(res["backup"])])
        copy = sqlite3.connect(f"file:{backups[0].as_posix()}?mode=ro", uri=True)
        try:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM meals WHERE id = ?", (meal["id"],)).fetchone()[0], 1)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM meal_items WHERE meal_id = ?",
                                          (meal["id"],)).fetchone()[0], 2)
        finally:
            copy.close()

    def test_backup_failure_blocks_delete(self):
        meal = nutrition.log_meal(self.conn, {"eaten_on": "2025-03-01", "items": [{"name": "Суп"}]})
        preview = records.prepare_delete(self.conn, "meal", meal["id"])
        with mock.patch("fitness.records.backup", side_effect=OSError("диск полон")):
            with self.assertRaises(BackupError) as ctx:
                records.delete_record(self.conn, "meal", meal["id"], preview["confirmation_code"],
                                      backup_dir=self.backups)
        self.assertIn("диск полон", str(ctx.exception))
        self.assertEqual(self.count("meals"), 1)
        self.assertEqual(self.count("meal_items"), 1)

    def test_delete_twice_is_not_found(self):
        note = notes.add_note(self.conn, {"note_date": "2025-03-01", "category": "общее", "text": "Тест"})
        preview = records.prepare_delete(self.conn, "note", note["id"])
        records.delete_record(self.conn, "note", note["id"], preview["confirmation_code"], backup_dir=self.backups)
        with self.assertRaises(NotFoundError):
            records.delete_record(self.conn, "note", note["id"], preview["confirmation_code"],
                                  backup_dir=self.backups)
        self.assertEqual(len(list(self.backups.glob("*.db"))), 1)


class BackupVerificationTests(TempDbTestCase):
    """db.verify_backup_file и её вызов в delete_record. Копии и их порча — только во временной папке."""

    def setUp(self):
        super().setUp()
        self.backups = self.tmp / "backups"
        self.meal = nutrition.log_meal(self.conn, {"eaten_on": "2025-03-01", "items": [{"name": "Суп"}]})
        self.updated_at = self.conn.execute("SELECT updated_at FROM meals WHERE id = ?",
                                            (self.meal["id"],)).fetchone()[0]

    def _copy(self):
        return db.backup(self.conn, self.backups, label="test")

    def _edit_copy(self, path, *statements):
        # Портим только временную копию; внешние ключи выключены, чтобы можно было записать «сироту».
        copy = sqlite3.connect(str(path), isolation_level=None)
        try:
            copy.execute("PRAGMA foreign_keys = OFF")
            for sql, params in statements:
                copy.execute(sql, params)
        finally:
            copy.close()

    def _verify(self, path, updated_at=None):
        db.verify_backup_file(path, "meals", self.meal["id"], updated_at or self.updated_at)

    def _delete_with_backup(self, fake_backup):
        preview = records.prepare_delete(self.conn, "meal", self.meal["id"])
        with mock.patch("fitness.records.backup", side_effect=fake_backup):
            with self.assertRaises(BackupError) as ctx:
                records.delete_record(self.conn, "meal", self.meal["id"], preview["confirmation_code"],
                                      backup_dir=self.backups)
        self.assertEqual(self.count("meals"), 1)
        self.assertEqual(self.count("meal_items"), 1)
        return ctx.exception

    def test_good_copy_passes(self):
        self._verify(self._copy())

    def test_copy_is_opened_read_only(self):
        path = self._copy()
        before = path.read_bytes()
        self._verify(path)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_copy(self):
        with self.assertRaises(BackupError):
            self._verify(self.backups / "nope.db")

    def test_damaged_copy(self):
        path = self._copy()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        with self.assertRaises(BackupError):
            self._verify(path)

    def test_garbage_file(self):
        path = self._copy()
        path.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(BackupError):
            self._verify(path)

    def test_foreign_key_violation(self):
        path = self._copy()
        self._edit_copy(path, ("INSERT INTO meal_items (meal_id, position, name) VALUES (?, 1, 'сирота')", (9999,)))
        with self.assertRaises(BackupError) as ctx:
            self._verify(path)
        self.assertIn("внешние ключи", str(ctx.exception))

    def test_record_missing_in_copy(self):
        path = self._copy()
        self._edit_copy(path, ("DELETE FROM meal_items WHERE meal_id = ?", (self.meal["id"],)),
                        ("DELETE FROM meals WHERE id = ?", (self.meal["id"],)))
        with self.assertRaises(BackupError) as ctx:
            self._verify(path)
        self.assertIn("нет в резервной копии", str(ctx.exception))

    def test_record_differs_in_copy(self):
        path = self._copy()
        with self.assertRaises(BackupError) as ctx:
            self._verify(path, updated_at="2099-01-01T00:00:00Z")
        self.assertIn("отличается", str(ctx.exception))

    def test_verification_error_is_backup_error(self):
        path = self._copy()
        with mock.patch("fitness.db._open_plain_readonly", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(BackupError) as ctx:
                self._verify(path)
        self.assertIn("disk I/O error", str(ctx.exception))

    def test_delete_passes_verification(self):
        preview = records.prepare_delete(self.conn, "meal", self.meal["id"])
        with mock.patch("fitness.records.verify_backup_file", wraps=db.verify_backup_file) as spy:
            res = records.delete_record(self.conn, "meal", self.meal["id"], preview["confirmation_code"],
                                        backup_dir=self.backups)
        spy.assert_called_once_with(Path(res["backup"]), "meals", self.meal["id"], self.updated_at)
        self.assertEqual(self.count("meals"), 0)

    def test_delete_refused_on_damaged_copy(self):
        def damaged(conn, backup_dir, label=None):
            path = db.backup(conn, backup_dir, label=label)
            path.write_bytes(path.read_bytes()[:4096])
            return path
        err = self._delete_with_backup(damaged)
        self.assertIsNotNone(err.backup)
        self.assertTrue(Path(err.backup).exists())    # неудачная копия не удаляется

    def test_delete_refused_when_record_missing_in_copy(self):
        def without_record(conn, backup_dir, label=None):
            path = db.backup(conn, backup_dir, label=label)
            self._edit_copy(path, ("DELETE FROM meal_items", ()), ("DELETE FROM meals", ()))
            return path
        self._delete_with_backup(without_record)

    def test_delete_refused_when_copy_fails(self):
        err = self._delete_with_backup(sqlite3.OperationalError("database is locked"))
        self.assertIsNone(err.backup)

    def test_delete_refused_when_verification_fails(self):
        preview = records.prepare_delete(self.conn, "meal", self.meal["id"])
        with mock.patch("fitness.db._open_plain_readonly", side_effect=OSError("нет доступа")):
            with self.assertRaises(BackupError):
                records.delete_record(self.conn, "meal", self.meal["id"], preview["confirmation_code"],
                                      backup_dir=self.backups)
        self.assertEqual(self.count("meals"), 1)


if __name__ == "__main__":
    unittest.main()
