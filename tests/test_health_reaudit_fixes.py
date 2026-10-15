"""Регрессионные тесты по результатам повторного аудита импортёра (дефекты Н1 и Н2)."""

import unittest
from unittest import mock

from fitness import health_import
from fitness.errors import ImportOrderError
from tests import health_samples as hs
from tests import test_health_import as ti


class RollbackThroughAllowOlderTests(ti.ImportTestCase):
    """Н1: порядок файлов и актуальность записи — разные вещи; --allow-older не должен откатывать содержимое."""

    def step_row(self):
        return self.rows("SELECT count, hc_last_modified, revision FROM step_intervals WHERE hc_id = 'X'")[0]

    def test_a_skipped_file_with_an_intermediate_time_does_not_roll_back_a_newer_version(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=10, modified="2026-10-10T10:00:00Z")]))
        # файл №3: тот же состав, но время изменения новее; время «виденной» версии должно запомниться
        self.run_import(hs.export(3, steps=[hs.step("X", count=10, modified="2026-10-10T12:00:00Z")]), allow_gaps=True)
        report = self.run_import(hs.export(2, steps=[hs.step("X", count=20, modified="2026-10-10T11:00:00Z")]), allow_older=True)
        self.assertEqual(report["status"], "imported")
        self.assertEqual(report["counts"]["steps"]["updated"], 0)
        self.assertEqual(report["not_applied"]["stale"], 1)
        row = self.step_row()
        self.assertEqual(row["count"], 10)
        self.assertTrue(row["hc_last_modified"].startswith("2026-10-10T12:00:00"))

    def test_a_newer_version_with_other_content_is_also_not_rolled_back(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=10, modified="2026-10-10T10:00:00Z")]))
        self.run_import(hs.export(3, steps=[hs.step("X", count=30, modified="2026-10-10T12:00:00Z")]), allow_gaps=True)
        self.run_import(hs.export(2, steps=[hs.step("X", count=20, modified="2026-10-10T11:00:00Z")]), allow_older=True)
        self.assertEqual(self.step_row()["count"], 30)

    def test_the_seen_time_is_remembered_without_creating_a_new_revision(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=10, modified="2026-10-10T10:00:00Z")]))
        before = self.step_row()["revision"]
        report = self.run_import(hs.export(2, steps=[hs.step("X", count=10, modified="2026-10-10T12:00:00Z")]))
        self.assertEqual(report["counts"]["steps"]["unchanged"], 1)
        self.assertEqual(self.step_row()["revision"], before)
        self.assertTrue(self.step_row()["hc_last_modified"].startswith("2026-10-10T12:00:00"))

    def test_an_older_time_never_lowers_the_stored_time(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=10, modified="2026-10-10T12:00:00Z")]))
        self.run_import(hs.export(2, steps=[hs.step("X", count=10, modified="2026-10-10T09:00:00Z")]))
        self.assertTrue(self.step_row()["hc_last_modified"].startswith("2026-10-10T12:00:00"))

    def test_heart_rate_and_sleep_remember_the_seen_time_too(self):
        samples = [{"time": "2026-10-10T19:10:00Z", "bpm": 60}]
        stages = [{"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:10:00Z"}]
        self.run_import(hs.export(1, heart_rate=[hs.heart("H", samples=samples, modified="2026-10-10T19:12:00Z")],
                                  sleep_sessions=[hs.sleep("Z", stages=stages, modified="2026-10-10T08:00:00Z")]))
        self.run_import(hs.export(3, heart_rate=[hs.heart("H", samples=samples, modified="2026-10-10T20:00:00Z")],
                                  sleep_sessions=[hs.sleep("Z", stages=stages, modified="2026-10-10T09:00:00Z")]), allow_gaps=True)
        other = [{"time": "2026-10-10T19:10:00Z", "bpm": 99}]
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("H", samples=other, modified="2026-10-10T19:30:00Z")],
                                           sleep_sessions=[hs.sleep("Z", stages=[], modified="2026-10-10T08:30:00Z")]), allow_older=True)
        self.assertEqual(report["counts"]["heart_rate"]["updated"], 0)
        self.assertEqual(report["counts"]["sleep"]["updated"], 0)
        self.assertEqual(self.rows("SELECT bpm FROM heart_rate_samples")[0]["bpm"], 60)
        self.assertEqual(self.count("sleep_stages"), 1)

    def test_a_skipped_file_with_a_genuinely_new_record_is_still_imported(self):
        self.run_import(hs.export(1, steps=[hs.step("X", count=10)]))
        self.run_import(hs.export(3, steps=[hs.step("X", count=10, modified="2026-10-10T12:00:00Z")]), allow_gaps=True)
        report = self.run_import(hs.export(2, steps=[hs.step("Y", "2026-10-10T02:00:00Z", "2026-10-10T02:10:00Z", count=5)]), allow_older=True)
        self.assertEqual(report["counts"]["steps"]["inserted"], 1)


class PendingDeletionTests(ti.ImportTestCase):
    """Н2: удаление пришло раньше записи (таблица health_pending_deletions, миграция 004)."""

    OLD = dict(start="2026-10-10T00:10:00Z", end="2026-10-10T00:20:00Z", count=50)

    def live(self, hc_id="X"):
        return self.rows("SELECT COUNT(*) AS n FROM step_intervals WHERE hc_id = ? AND deleted_at IS NULL", hc_id)[0]["n"]

    def pending(self):
        return self.rows("SELECT * FROM health_pending_deletions ORDER BY id")

    def deletion_first(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        report = self.run_import(hs.export(3, deleted=["X"]), allow_gaps=True)
        self.assertEqual(len(report["unmatched_ids"]), 1)
        return report

    def deletion_time(self):
        return self.rows("SELECT exported_at FROM import_batches WHERE sequence = 3")[0]["exported_at"]

    def test_a_deletion_before_the_record_keeps_a_pending_row(self):
        self.deletion_first()
        rows = self.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["hc_id"], rows[0]["source_key"] is not None, rows[0]["phone_id"]), ("X", True, None))   # область — источник файла

    def test_the_skipped_older_file_does_not_bring_the_record_back(self):
        self.deletion_first()
        report = self.run_import(hs.export(2, steps=[hs.step("X", **self.OLD)]), allow_older=True)
        self.assertEqual(report["counts"]["steps"]["inserted"], 0)
        self.assertEqual(report["not_applied"]["replay_of_deleted"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM step_intervals WHERE hc_id = 'X'")[0]["n"], 0)
        self.assertEqual(len(self.pending()), 1)                       # ожидающее удаление не снято: безопасно держать дальше

    def test_repeating_the_file_or_a_second_skipped_file_does_not_bring_it_back(self):
        self.deletion_first()
        doc = hs.export(2, steps=[hs.step("X", **self.OLD)])
        self.run_import(doc, allow_older=True)
        self.assertEqual(self.run_import(doc, name="again.json", allow_older=True)["status"], "already_imported")
        other = hs.export(2, phone="phone-01", steps=[hs.step("X", **self.OLD)], exported_at="2026-10-10T05:00:00Z")
        with self.assertRaises(ImportOrderError):                      # номер 2 уже принят с другим содержимым
            self.run_import(other, allow_older=True)
        self.assertEqual(self.live(), 0)
        self.assertEqual(len(self.pending()), 1)

    def test_a_file_exported_at_the_same_moment_as_the_deletion_is_still_a_replay(self):
        self.deletion_first()
        self.run_import(hs.export(2, steps=[hs.step("X", **self.OLD)], exported_at=self.deletion_time()), allow_older=True)
        self.assertEqual(self.live(), 0)

    def test_a_genuinely_newer_export_brings_the_record_back_and_clears_the_pending_row(self):
        self.deletion_first()
        later = "2026-10-10T23:50:00Z"
        self.assertGreater(later, self.deletion_time())
        report = self.run_import(hs.export(4, exported_at=later, steps=[hs.step("X", **self.OLD)]))
        self.assertEqual(report["counts"]["steps"]["inserted"], 1)
        self.assertEqual(self.live(), 1)
        self.assertEqual(self.pending(), [])

    def test_the_same_deletion_in_a_different_file_keeps_one_row_with_the_latest_export_time(self):
        self.deletion_first()
        self.run_import(hs.export(4, deleted=["X"], exported_at="2026-10-10T22:00:00Z"))
        rows = self.pending()
        self.assertEqual((len(rows), rows[0]["exported_at"]), (1, "2026-10-10T22:00:00Z"))
        self.run_import(hs.export(5, deleted=["X"], exported_at="2026-10-10T21:00:00Z"))          # более раннее время не откатывает
        self.assertEqual(self.pending()[0]["exported_at"], "2026-10-10T22:00:00Z")

    def test_repeating_the_deletion_file_is_idempotent(self):
        doc = hs.export(1, deleted=["X"])
        self.run_import(doc)
        before = self.dump()
        self.assertEqual(self.run_import(doc, name="copy.json")["status"], "already_imported")
        self.assertEqual(self.dump(), before)

    def test_scope_by_declared_source_blocks_other_phones_with_that_source_only(self):
        src = hs.source("s1", "app.one")
        self.run_import(hs.export(1, phone="phone-A", deleted=["X"], sources=[src]))
        row = self.pending()[0]
        self.assertEqual((row["source_key"] is not None, row["phone_id"]), (True, None))
        self.run_import(hs.export(1, phone="phone-B", steps=[hs.step("X", src="s1", **self.OLD)], sources=[src]))
        self.assertEqual(self.live(), 0)                                                            # тот же источник: блок
        self.run_import(hs.export(2, phone="phone-B", steps=[hs.step("X", src="s2", **self.OLD)], sources=[hs.source("s2", "app.two")]))
        self.assertEqual(self.live(), 1)                                                            # другой источник: запись своя

    def test_scope_by_phone_when_the_deletion_declares_no_sources(self):
        same = "2026-10-10T12:00:00Z"
        self.run_import(hs.export(1, phone="phone-A", deleted=["X"], sources=[], exported_at=same))
        self.assertEqual((self.pending()[0]["source_key"], self.pending()[0]["phone_id"]), (None, "phone-A"))
        self.run_import(hs.export(1, phone="phone-B", steps=[hs.step("X", **self.OLD)], exported_at=same))
        self.assertEqual(self.live(), 1)                                                            # чужой телефон: не блокируется
        self.run_import(hs.export(2, phone="phone-A", steps=[hs.step("Y", **self.OLD)], exported_at=same))
        self.assertEqual(self.live("Y"), 1)                                                         # другой id: не затронут
        self.run_import(hs.export(3, phone="phone-A", deleted=["W"], sources=[], exported_at=same))
        report = self.run_import(hs.export(4, phone="phone-A", steps=[hs.step("W", **self.OLD)], exported_at=same))
        self.assertEqual((report["not_applied"]["replay_of_deleted"], self.live("W")), (1, 0))      # свой телефон: блок

    def test_a_dry_run_and_a_rejected_file_leave_no_pending_row(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        self.assertEqual(self.run_import(hs.export(2, deleted=["X"]), dry_run=True)["status"], "dry_run")
        self.assertEqual(self.pending(), [])
        with self.assertRaises(ImportOrderError):
            self.run_import(hs.export(5, deleted=["X"]))                                           # пропуск номеров без ключа
        self.assertEqual(self.pending(), [])

    def test_an_interrupted_import_rolls_back_the_pending_row(self):
        self.run_import(hs.export(1, steps=[hs.step("a")]))
        with mock.patch.object(health_import, "_upsert_records", side_effect=RuntimeError("обрыв")):
            with self.assertRaises(Exception):
                self.run_import(hs.export(2, deleted=["X"], steps=[hs.step("b", start="2026-10-10T03:00:00Z", end="2026-10-10T03:10:00Z")]))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM import_batches WHERE status = 'ok' AND sequence = 2")[0]["n"], 0)
        report = self.run_import(hs.export(2, deleted=["X"]))                                       # после сбоя файл импортируется
        self.assertEqual((report["status"], len(self.pending())), ("imported", 1))

    def test_a_deletion_of_an_existing_record_creates_no_pending_row(self):
        self.run_import(hs.export(1, steps=[hs.step("X", **self.OLD)]))
        self.run_import(hs.export(2, deleted=["X"]))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.live(), 0)

    def test_heart_rate_and_sleep_are_covered_too(self):
        same = "2026-10-10T12:00:00Z"
        self.run_import(hs.export(1, deleted=["H", "Z"], exported_at=same))
        stages = [{"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:10:00Z"}]
        report = self.run_import(hs.export(2, heart_rate=[hs.heart("H", samples=[{"time": "2026-10-10T19:10:00Z", "bpm": 60}])],
                                           sleep_sessions=[hs.sleep("Z", stages=stages)],
                                           exported_at=same))
        self.assertEqual(report["not_applied"]["replay_of_deleted"], 2)
        self.assertEqual(self.count("heart_rate_records") + self.count("sleep_sessions"), 0)

    def test_unrelated_records_from_the_skipped_file_are_still_imported(self):
        self.deletion_first()
        report = self.run_import(hs.export(2, steps=[hs.step("X", **self.OLD), hs.step("Y", start="2026-10-10T01:00:00Z", end="2026-10-10T01:10:00Z", count=3)]),
                                 allow_older=True)
        self.assertEqual((report["counts"]["steps"]["inserted"], report["not_applied"]["replay_of_deleted"]), (1, 1))
        self.assertEqual((self.live("X"), self.live("Y")), (0, 1))


class LateDeletionAfterRepeatTests(ti.ImportTestCase):
    """Н3: повторная запись с тем же содержимым в более позднем файле подтверждает её; старое удаление её не удаляет."""

    T2, T3, T7 = "2026-10-10T02:00:00Z", "2026-10-10T03:00:00Z", "2026-10-10T07:00:00Z"

    def row(self, hc_id="X"):
        return self.rows("SELECT s.deleted_at, s.revision, b.sequence, b.exported_at FROM step_intervals s "
                         "JOIN import_batches b ON b.id = s.batch_id WHERE s.hc_id = ?", hc_id)[0]

    def record(self, sequence, exported_at, count=10, modified="2026-10-10T01:00:00Z", hc_id="X"):
        return hs.export(sequence, exported_at=exported_at, steps=[hs.step(hc_id, count=count, modified=modified)])

    def test_a_late_older_deletion_does_not_delete_a_record_confirmed_by_a_later_file(self):
        self.run_import(self.record(1, self.T2))
        self.run_import(self.record(2, self.T7))                       # то же содержимое, экспорт позже
        report = self.run_import(hs.export(3, deleted=["X"], exported_at=self.T3), allow_gaps=True)
        self.assertEqual((report["counts"]["steps"]["deleted"], report["not_applied"]["deletion_stale"]), (0, 1))
        self.assertIsNone(self.row()["deleted_at"])

    def test_the_same_sequence_with_the_skipped_deletion_found_afterwards(self):
        self.run_import(self.record(1, self.T2))
        self.run_import(self.record(3, self.T7), allow_gaps=True)
        report = self.run_import(hs.export(2, deleted=["X"], exported_at=self.T3), allow_older=True)
        self.assertEqual((report["counts"]["steps"]["deleted"], report["not_applied"]["deletion_stale"]), (0, 1))
        self.assertIsNone(self.row()["deleted_at"])

    def test_the_repeat_moves_the_batch_binding_without_a_new_revision(self):
        self.run_import(self.record(1, self.T2))
        before = self.row()
        self.run_import(self.record(2, self.T7))
        after = self.row()
        self.assertEqual((before["sequence"], after["sequence"]), (1, 2))
        self.assertEqual((after["exported_at"], after["revision"]), (self.T7, before["revision"]))

    def test_an_older_repeat_never_moves_the_binding_back(self):
        self.run_import(self.record(1, self.T7))
        self.run_import(self.record(2, self.T2))
        self.assertEqual(self.row()["sequence"], 1)
        report = self.run_import(hs.export(3, deleted=["X"], exported_at=self.T3))
        self.assertEqual(report["not_applied"]["deletion_stale"], 1)

    def test_a_genuinely_later_deletion_still_deletes(self):
        self.run_import(self.record(1, self.T2))
        self.run_import(self.record(2, self.T3))
        report = self.run_import(hs.export(3, deleted=["X"], exported_at=self.T7))
        self.assertEqual(report["counts"]["steps"]["deleted"], 1)
        self.assertIsNotNone(self.row()["deleted_at"])

    def test_a_deletion_exported_before_the_first_file_still_cannot_delete_it(self):
        self.run_import(self.record(2, self.T3))
        report = self.run_import(hs.export(1, deleted=["X"], exported_at=self.T2), allow_older=True)
        self.assertEqual(report["not_applied"]["deletion_stale"], 1)

    def test_a_normal_update_and_the_repeat_of_the_same_file_keep_working(self):
        self.run_import(self.record(1, self.T2))
        report = self.run_import(self.record(2, self.T7, count=20, modified="2026-10-10T05:00:00Z"))
        self.assertEqual(report["counts"]["steps"]["updated"], 1)
        self.assertEqual(self.rows("SELECT count FROM step_intervals")[0]["count"], 20)
        doc = self.record(3, "2026-10-10T08:00:00Z", count=20, modified="2026-10-10T05:00:00Z")
        self.run_import(doc)
        before = self.dump()
        self.assertEqual(self.run_import(doc, name="copy.json")["status"], "already_imported")
        self.assertEqual(self.dump(), before)

    def test_a_repeat_of_a_pending_deleted_record_is_not_affected(self):
        self.run_import(hs.export(1, deleted=["X"], exported_at=self.T3))
        self.run_import(self.record(2, self.T2))                                                   # старый файл: не восстанавливает
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM step_intervals")[0]["n"], 0)
        self.run_import(self.record(3, self.T7))                                                   # новее удаления: возвращает
        self.assertIsNone(self.row()["deleted_at"])
        self.run_import(self.record(4, "2026-10-10T08:00:00Z"))
        report = self.run_import(hs.export(5, deleted=["X"], exported_at=self.T3), allow_gaps=True)
        self.assertEqual(report["not_applied"]["deletion_stale"], 1)

    def test_the_mismatch_check_with_other_records_in_the_same_files(self):
        self.run_import(hs.export(1, exported_at=self.T2, steps=[hs.step("X", count=1, modified="2026-10-10T01:00:00Z"),
                                                                  hs.step("Y", start="2026-10-10T01:00:00Z", end="2026-10-10T01:10:00Z", count=2)]))
        self.run_import(hs.export(2, exported_at=self.T7, steps=[hs.step("X", count=1, modified="2026-10-10T01:00:00Z")]))
        report = self.run_import(hs.export(3, deleted=["X", "Y"], exported_at=self.T3), allow_gaps=True)
        self.assertEqual((report["counts"]["steps"]["deleted"], report["not_applied"]["deletion_stale"]), (1, 1))
        self.assertEqual(self.rows("SELECT hc_id FROM step_intervals WHERE deleted_at IS NULL")[0]["hc_id"], "X")


if __name__ == "__main__":
    unittest.main()
