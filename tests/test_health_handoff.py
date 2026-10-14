"""Передача файлов из папки Syncthing (data\\health_syncthing) во входящую папку импорта.

Главное требование: папка Syncthing «Только приём» никогда не меняется (иначе Syncthing требует «Отменить локальные
изменения»), а каждый файл из неё ровно один раз доходит до обычного импорта.

Все базы и папки временные, все данные синтетические. Рабочая база проекта не используется.
"""

import contextlib
import hashlib
import io
import json
import os
import time
from unittest import mock

import health_inbox as cli
from fitness import health_handoff, health_import, health_inbox, health_status
from tests import health_samples as hs
from tests.test_health_inbox import OLD, InboxTestCase, day

LATER = lambda: time.time() + 3600      # noqa: E731 — «через час»: файл давно дописан


class HandoffTestCase(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.dirs["syncthing"]
        (self.src / ".stfolder").mkdir(parents=True)                # метка папки Syncthing

    def sync(self, name, doc, mtime=OLD):
        """Файл, который Syncthing положил в свою папку (время изменения Syncthing переносит с телефона)."""
        path = hs.write(self.src, name, doc)
        os.utime(path, (mtime, mtime))
        return path

    def run_inbox(self, **kw):
        kw.setdefault("clock", LATER)
        return super().run_inbox(**kw)

    def source_state(self):
        """Всё содержимое папки Syncthing: имена (включая скрытые), размер, время изменения, SHA-256."""
        state = {}
        for path in sorted(self.src.rglob("*")):
            st = path.stat()
            state[str(path.relative_to(self.src))] = (
                None if path.is_dir() else (st.st_size, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()))
        return state

    def names(self, key):
        folder = self.dirs[key]
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def ledger(self):
        return health_handoff._read_ledger_quiet(self.dirs["state"])

    def assertSourceUntouched(self, before):
        self.assertEqual(self.source_state(), before)


class NewFileTest(HandoffTestCase):
    def test_new_file_is_copied_imported_and_source_kept(self):
        self.sync("a.json", day(1, "2026-10-01"))
        before = self.source_state()
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["copied"], [{"file": "a.json", "inbox_name": "a.json"}])
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertEqual(self.names("processed"), ["a.json"])
        self.assertEqual(self.names("inbox"), [])
        self.assertSourceUntouched(before)
        entry = next(iter(self.ledger().values()))
        self.assertEqual((entry["name"], entry["result"]), ("a.json", "copied"))
        # Копия — байт в байт исходный файл, с тем же временем изменения.
        copy = self.dirs["processed"] / "a.json"
        self.assertEqual(copy.read_bytes(), (self.src / "a.json").read_bytes())
        self.assertEqual(copy.stat().st_mtime_ns, (self.src / "a.json").stat().st_mtime_ns)
        self.assertEqual(health_inbox.exit_code(run), 0)
        self.assertIn("Syncthing → входящая: a.json", "\n".join(health_inbox.describe_run(run)))

    def test_next_runs_do_nothing(self):
        self.sync("a.json", day(1, "2026-10-01"))
        self.run_inbox()
        before = self.source_state()
        with mock.patch.object(health_handoff, "_copy") as copy:
            run = self.run_inbox()
        copy.assert_not_called()
        self.assertEqual((run["handoff"]["known"], run["handoff"]["copied"], run["files"]), (1, [], []))
        self.assertEqual(self.batches(), [(1, "ok")])
        self.assertSourceUntouched(before)

    def test_files_arriving_in_order(self):
        self.sync("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.sync("2.json", day(2, "2026-10-02"))
        self.sync("3.json", day(3, "2026-10-03"))
        run = self.run_inbox()
        self.assertEqual([i["file"] for i in run["handoff"]["copied"]], ["2.json", "3.json"])
        self.assertEqual(self.batches(), [(1, "ok"), (2, "ok"), (3, "ok")])

    def test_syncthing_temporary_and_hidden_files_are_ignored(self):
        hs.write(self.src, ".syncthing.a.json.tmp", b"{")
        hs.write(self.src, "~a.json", b"{")
        hs.write(self.src / "sub", "nested.json", day(1, "2026-10-01"))
        before = self.source_state()
        run = self.run_inbox()
        self.assertEqual((run["handoff"]["copied"], run["files"]), ([], []))
        self.assertSourceUntouched(before)

    def test_no_syncthing_folder_is_not_an_error(self):
        for path in sorted(self.src.rglob("*"), reverse=True):
            path.rmdir()
        self.src.rmdir()
        self.put("a.json", hs.day_file(1))
        run = self.run_inbox()
        self.assertFalse(run["handoff"]["exists"])
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertFalse(self.src.exists())                                    # импорт её не создаёт


class StillWritingTest(HandoffTestCase):
    def test_freshly_created_file_waits(self):
        self.sync("a.json", day(1, "2026-10-01"))
        run = self.run_inbox(clock=time.time)                 # создан только что
        self.assertEqual([i["file"] for i in run["handoff"]["busy"]], ["a.json"])
        self.assertEqual((self.names("inbox"), self.ledger()), ([], {}))
        run = self.run_inbox()                                # прошло время — передан
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})

    def test_file_growing_during_check_waits(self):
        path = self.sync("a.json", b'{"format_version": 1,')

        def grow(_seconds):
            with open(path, "ab") as fh:
                fh.write(b' "more": 1')
        run = self.run_inbox(sleep=grow, stable_wait=1)
        self.assertEqual([i["file"] for i in run["handoff"]["busy"]], ["a.json"])
        self.assertEqual(self.names("inbox"), [])
        self.assertEqual(self.names("failed"), [])

    def test_truncated_json_waits_then_goes_through(self):
        doc = hs.encode(day(1, "2026-10-01"))
        path = self.sync("a.json", doc[:len(doc) // 2])
        now = time.time()
        run = self.run_inbox(clock=lambda: now + 10)          # моложе минуты: ещё пишут
        self.assertEqual(run["handoff"]["busy"][0]["reason"], "недописанный JSON, проверю позже")
        self.assertEqual((self.names("inbox"), self.names("failed")), ([], []))
        path.write_bytes(doc)
        os.utime(path, (OLD, OLD + 1))
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})

    def test_file_changed_while_reading_waits(self):
        self.sync("a.json", day(1, "2026-10-01"))
        calls = []

        def stamp(path):
            calls.append(1)
            size, mtime_ns, _ = _old_stamp(path)
            # Два замера до чтения совпали, а замер сразу после чтения показал другое время изменения.
            return size, mtime_ns + (1 if len(calls) == 3 else 0), OLD
        with mock.patch.object(health_handoff, "_stamp", side_effect=stamp):
            run = self.run_inbox()
        self.assertEqual(run["handoff"]["busy"][0]["reason"], "файл изменился во время чтения")
        self.assertEqual(self.names("inbox"), [])
        self.assertEqual(self.outcomes(self.run_inbox()), {"a.json": "imported"})


class RepeatedFileTest(HandoffTestCase):
    def test_same_content_under_another_name_is_not_imported_again(self):
        self.sync("a.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.sync("a (copy).json", (self.src / "a.json").read_bytes(), mtime=OLD + 50)
        run = self.run_inbox()
        self.assertEqual((run["handoff"]["copied"], run["files"]), ([], []))
        self.assertEqual(run["handoff"]["known"], 2)
        self.assertEqual(self.names("processed"), ["a.json"])
        self.run_inbox()                                       # псевдоним запомнен: файл больше не читается
        self.assertEqual(self.run_inbox()["handoff"]["known"], 2)

    def test_file_already_in_database_is_not_copied(self):
        # Например, после перенастройки Syncthing заново скачал все файлы с телефона, а импорт был раньше.
        self.put("a.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.sync("a.json", (self.dirs["processed"] / "a.json").read_bytes())
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["already_in_db"], ["a.json"])
        self.assertEqual((self.names("inbox"), self.names("processed")), ([], ["a.json"]))
        self.assertEqual(next(iter(self.ledger().values()))["result"], "already_in_db")
        self.assertEqual(self.batches(), [(1, "ok")])

    def test_lost_ledger_does_not_duplicate_imports(self):
        self.sync("a.json", day(1, "2026-10-01"))
        self.run_inbox()
        (self.dirs["state"] / health_handoff.LEDGER_FILE).unlink()
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["already_in_db"], ["a.json"])
        self.assertEqual(self.names("processed"), ["a.json"])

    def test_corrupt_ledger_is_kept_aside(self):
        self.sync("a.json", day(1, "2026-10-01"))
        self.run_inbox()
        (self.dirs["state"] / health_handoff.LEDGER_FILE).write_text("{oops", encoding="utf-8")
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["already_in_db"], ["a.json"])
        self.assertTrue(any(n.startswith(health_handoff.LEDGER_FILE + ".corrupt-") for n in self.names("state")))


class ImportErrorTest(HandoffTestCase):
    def test_bad_file_goes_to_failed_once_and_source_stays(self):
        self.sync("bad.json", b"{")
        before = self.source_state()
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"bad.json": "failed"})
        self.assertEqual(self.names("failed"), ["bad.json", "bad.json.error.txt"])
        self.assertEqual(health_inbox.exit_code(run), 1)
        run = self.run_inbox()                                 # повторно не передаётся
        self.assertEqual((run["handoff"]["known"], run["files"]), (1, []))
        self.assertSourceUntouched(before)

    def test_retry_failed_works_and_does_not_recopy(self):
        self.sync("bad.json", b"{")
        self.run_inbox()
        self.assertEqual(health_inbox.retry_failed(self.db_path), ["bad.json"])
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["copied"], [])
        self.assertEqual(self.outcomes(run), {"bad.json": "failed"})
        self.assertEqual(self.names("inbox"), [])

    def test_held_file_waits_in_inbox_not_in_syncthing_folder(self):
        self.sync("1.json", day(1, "2026-10-01"))
        self.run_inbox()
        self.sync("3.json", day(3, "2026-10-05"))
        before = self.source_state()
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"3.json": "held"})
        self.assertEqual(self.names("inbox"), ["3.json"])
        run = self.run_inbox()                                 # ждёт, но не копируется повторно
        self.assertEqual(run["handoff"]["copied"], [])
        self.assertEqual(self.names("inbox"), ["3.json"])
        self.assertSourceUntouched(before)

    def test_copy_error_is_reported_and_retried(self):
        self.sync("a.json", day(1, "2026-10-01"))
        before = self.source_state()
        with mock.patch.object(health_handoff, "_copy", side_effect=OSError("диск переполнен")):
            run = self.run_inbox()
        self.assertIn("диск переполнен", run["handoff"]["errors"][0]["error"])
        self.assertEqual(health_inbox.exit_code(run), 1)
        self.assertEqual(self.ledger(), {})
        log = (self.dirs["state"] / health_inbox.RUNS_LOG).read_text(encoding="utf-8")
        self.assertIn("диск переполнен", log)                  # ошибка записана в журнал запусков
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertSourceUntouched(before)

    def test_partial_copy_leaves_no_file_in_inbox(self):
        self.sync("a.json", day(1, "2026-10-01"))
        with mock.patch.object(health_handoff.os, "replace", side_effect=OSError("сбой")):
            run = self.run_inbox()
        self.assertTrue(run["handoff"]["errors"])
        self.assertEqual(self.names("inbox"), [])              # временный файл убран

    def test_database_problem_keeps_copy_in_inbox(self):
        self.sync("a.json", day(1, "2026-10-01"))
        with mock.patch.object(health_import, "import_file",
                               side_effect=health_inbox.TrackerError("database is locked")):
            run = self.run_inbox()
        self.assertIn("locked", run["error"]["message"])
        self.assertEqual(self.names("inbox"), ["a.json"])
        run = self.run_inbox()
        self.assertEqual(run["handoff"]["copied"], [])         # копия уже есть, вторая не нужна
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})


class RestartTest(HandoffTestCase):
    def test_crash_after_copy_before_ledger(self):
        self.sync("a.json", day(1, "2026-10-01"))
        with mock.patch.object(health_handoff, "save_ledger", side_effect=RuntimeError("питание пропало")):
            with self.assertRaises(RuntimeError):
                self.run_inbox()
        self.assertEqual(self.names("inbox"), ["a.json"])      # копия успела записаться, журнала нет
        run = self.run_inbox()                                 # перезапуск
        self.assertEqual(run["handoff"]["copied"], [{"file": "a.json", "inbox_name": "a.json", "existing_copy": True}])
        self.assertEqual(self.outcomes(run), {"a.json": "imported"})
        self.assertEqual((self.names("inbox"), self.names("processed")), ([], ["a.json"]))
        self.assertEqual(self.batches(), [(1, "ok")])

    def test_crash_after_copy_and_import_before_move(self):
        self.sync("a.json", day(1, "2026-10-01"))
        with mock.patch.object(health_inbox, "_move", side_effect=OSError("файл занят")):
            self.run_inbox()
        run = self.run_inbox()
        self.assertEqual(self.outcomes(run), {"a.json": "already_imported"})
        self.assertEqual((self.names("inbox"), self.names("processed")), ([], ["a.json"]))

    def test_second_processor_does_not_copy(self):
        self.sync("a.json", day(1, "2026-10-01"))
        with health_inbox._Lock(self.dirs["state"] / health_inbox.LOCK_FILE):
            run = self.run_inbox()
        self.assertEqual(run["error"]["code"], "inbox_locked")
        self.assertEqual((self.names("inbox"), self.ledger()), ([], {}))
        self.assertEqual(self.outcomes(self.run_inbox()), {"a.json": "imported"})

    def test_watch_hands_off_and_reports_once(self):
        self.sync("a.json", day(1, "2026-10-01"))
        lines = []
        real = health_inbox.process_inbox
        with mock.patch.object(health_inbox, "process_inbox",
                               side_effect=lambda *a, **kw: real(*a, **(kw | {"clock": LATER, "stable_wait": 0}))):
            health_inbox.watch(self.db_path, 5, out=lines.append, sleep=lambda s: None, max_runs=3)
        text = "\n".join(lines)
        self.assertEqual(text.count("Syncthing → входящая: a.json"), 1)
        self.assertEqual(self.names("processed"), ["a.json"])


class NameConflictTest(HandoffTestCase):
    def test_same_name_already_in_inbox(self):
        self.put("x.json", day(1, "2026-10-01"), mtime=OLD)
        self.sync("x.json", day(2, "2026-10-02"))
        run = self.run_inbox()
        copied = run["handoff"]["copied"][0]
        self.assertEqual(copied["file"], "x.json")
        self.assertNotEqual(copied["inbox_name"], "x.json")
        self.assertEqual(self.batches(), [(1, "ok"), (2, "ok")])
        self.assertEqual(len(self.names("processed")), 2)

    def test_same_name_new_content_in_syncthing_folder(self):
        path = self.sync("x.json", day(1, "2026-10-01"))
        self.run_inbox()
        path.write_bytes(hs.encode(day(2, "2026-10-02")))      # телефон прислал другой файл под тем же именем
        os.utime(path, (OLD + 100, OLD + 100))
        run = self.run_inbox()
        self.assertEqual(len(run["handoff"]["copied"]), 1)
        self.assertEqual(self.batches(), [(1, "ok"), (2, "ok")])
        self.assertEqual(len(self.names("processed")), 2)       # прежняя копия не перезаписана


class ModesTest(HandoffTestCase):
    def test_dry_run_checks_without_copying(self):
        self.sync("a.json", day(1, "2026-10-01"))
        before = self.source_state()
        run = self.run_inbox(dry_run=True)
        self.assertEqual(run["handoff"]["would_copy"], ["a.json"])
        self.assertEqual(self.outcomes(run), {"a.json": "dry_run"})
        self.assertEqual((self.names("inbox"), self.batches()), ([], []))
        self.assertFalse((self.dirs["state"] / health_handoff.LEDGER_FILE).exists())
        self.assertSourceUntouched(before)

    def test_status_shows_syncthing_folder(self):
        self.sync("a.json", day(1, "2026-10-01"))
        status = health_inbox.inbox_status(self.db_path)
        self.assertEqual((status["syncthing"]["exists"], status["syncthing"]["files"],
                          status["syncthing"]["not_handed_off"]), (True, 1, 1))
        self.run_inbox()
        full = health_status.sync_status(self.db_path)
        self.assertEqual(full["inbox"]["syncthing"]["not_handed_off"], 0)
        self.assertIn("Папка Syncthing:", "\n".join(health_status.describe(full)))
        self.assertEqual(full["inbox"]["last_run"]["handoff"]["copied"], ["a.json"])

    def test_status_warns_about_old_setup(self):
        (self.src / ".stfolder").rmdir()
        (self.dirs["inbox"] / ".stfolder").mkdir(parents=True)
        status = health_status.sync_status(self.db_path)
        self.assertTrue(status["inbox"]["syncthing"]["old_setup"])
        self.assertTrue(any("health_syncthing" in n for n in status["attention"]))

    def test_cli_modes(self):
        self.sync("a.json", day(1, "2026-10-01"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--db", str(self.db_path), "--status"]), 0)
        self.assertIn("Папка Syncthing", out.getvalue())
        with mock.patch.object(health_inbox, "split_ready", side_effect=lambda paths, **kw: (list(paths), [])), \
                mock.patch.object(health_inbox, "MIN_AGE_SEC", 0), \
                mock.patch.object(health_inbox, "STABLE_WAIT_SEC", 0):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(health_handoff, "_stamp",
                                                                    side_effect=lambda p: _old_stamp(p)):
                self.assertEqual(cli.main(["--db", str(self.db_path), "--dry-run", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["handoff"]["would_copy"], ["a.json"])
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(health_handoff, "_stamp",
                                                                              side_effect=lambda p: _old_stamp(p)):
                self.assertEqual(cli.main(["--db", str(self.db_path)]), 0)
                self.assertEqual(cli.main(["--db", str(self.db_path), "--retry-failed"]), 0)
        self.assertEqual(self.batches(), [(1, "ok")])


def _old_stamp(path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns, OLD
