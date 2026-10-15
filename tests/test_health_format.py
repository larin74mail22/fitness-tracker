"""Формат файла выгрузки Health Connect v1: разбор, нормализация времени и проверка. Базы здесь нет."""

import copy
import unittest
from unittest import mock

from fitness import health_format as hf
from fitness.errors import ImportFileError
from tests import health_samples as hs


def parse(doc):
    return hf.parse_export(hs.encode(doc) if not isinstance(doc, bytes) else doc)


def problems_of(doc):
    try:
        parse(doc)
    except ImportFileError as exc:
        return exc
    raise AssertionError("файл должен был не пройти проверку")


class ValidFileTests(unittest.TestCase):
    def test_minimal_file_with_only_header(self):
        parsed = parse(hs.export(sources=[]))
        self.assertEqual(parsed.header["phone_id"], "phone-01")
        self.assertEqual(parsed.header["sequence"], 1)
        self.assertEqual({k: len(v) for k, v in parsed.records.items()}, {"steps": 0, "heart_rate": 0, "sleep": 0})

    def test_lists_may_be_omitted(self):
        doc = hs.export()
        for key in ("steps", "heart_rate", "sleep", "deleted", "sources"):
            doc.pop(key)
        self.assertEqual(parse(doc).deleted, [])

    def test_example_day(self):
        parsed = parse(hs.day_file())
        self.assertEqual([len(parsed.records[k]) for k in hf.RECORD_TYPES], [3, 3, 1])
        self.assertEqual(len(parsed.records["sleep"][0]["stages"]), 8)
        self.assertEqual(parsed.records["steps"][0]["local_date"], "2026-10-10")
        self.assertEqual(len(parsed.sha256), 64)

    def test_unicode_and_bom(self):
        doc = hs.day_file()
        doc["sleep"][0]["title"] = "Сон после смены — ёж"
        data = b"\xef\xbb\xbf" + hs.encode(doc)
        self.assertEqual(parse(data).records["sleep"][0]["title"], "Сон после смены — ёж")


class TimestampTests(unittest.TestCase):
    def one(self, value):
        problems = hf.Problems()
        return hf.parse_timestamp(value, "t", problems), problems

    def test_fractions_are_padded_to_nine_digits(self):
        cases = {
            "2026-10-10T07:02:00Z": "2026-10-10T07:02:00.000000000Z",
            "2026-10-10T07:02:00.5Z": "2026-10-10T07:02:00.500000000Z",
            "2026-10-10T07:02:00.123Z": "2026-10-10T07:02:00.123000000Z",
            "2026-10-10T07:02:00.123456Z": "2026-10-10T07:02:00.123456000Z",
            "2026-10-10T07:02:00.123456789Z": "2026-10-10T07:02:00.123456789Z",
            "2026-10-10T07:02:00.000000001Z": "2026-10-10T07:02:00.000000001Z",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                value, problems = self.one(given)
                self.assertEqual(value, expected)
                self.assertFalse(problems)

    def test_same_instant_in_different_spellings_is_equal(self):
        a, _ = self.one("2026-10-10T07:02:00Z")
        b, _ = self.one("2026-10-10T07:02:00.000Z")
        c, _ = self.one("2026-10-10T07:02:00.000000000Z")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_canonical_strings_sort_chronologically(self):
        raw = ["2026-10-10T07:02:00Z", "2026-10-10T07:02:00.000000001Z", "2026-10-10T07:02:00.1Z",
               "2026-10-10T07:02:00.5Z", "2026-10-10T07:02:01Z", "2026-10-10T07:02:01.000000007Z"]
        canon = [self.one(v)[0] for v in raw]
        self.assertEqual(sorted(canon), canon)

    def test_rejected_forms(self):
        bad = ["2026-10-10 07:02:00Z", "2026-10-10T07:02:00", "2026-10-10T07:02:00+03:00", "2026-10-10T07:02:00z",
               "2026-10-10T07:02:00.Z", "2026-10-10T07:02:00.1234567890Z", "2026-10-10T07:02Z", "2026-13-01T00:00:00Z",
               "2026-02-30T00:00:00Z", "2026-10-10T24:00:00Z", "2026-10-10T07:02:60Z", "1969-12-31T23:59:59Z",
               "2101-01-01T00:00:00Z", "２０２６-10-10T07:02:00Z", "", None, 1760083500, True, ["x"]]
        for value in bad:
            with self.subTest(value=value):
                result, problems = self.one(value)
                self.assertIsNone(result)
                self.assertTrue(problems)


class LocalDateTests(unittest.TestCase):
    def test_offsets_move_the_date(self):
        self.assertEqual(hf.local_date("2026-10-10T22:30:00.000000000Z", 180), "2026-10-11")
        self.assertEqual(hf.local_date("2026-10-10T22:30:00.000000000Z", 0), "2026-10-10")
        self.assertEqual(hf.local_date("2026-10-10T01:00:00.000000000Z", -300), "2026-10-09")
        self.assertEqual(hf.local_date("2026-12-31T23:59:59.999999999Z", 60), "2027-01-01")

    def test_record_offset_beats_fallback_and_fallback_fills_gaps(self):
        doc = hs.export(steps=[hs.step(start="2026-10-10T22:00:00Z", end="2026-10-10T22:10:00Z", offset=180)])
        self.assertEqual(parse(doc).records["steps"][0]["local_date"], "2026-10-11")
        doc = hs.export(steps=[hs.step(start="2026-10-10T22:00:00Z", end="2026-10-10T22:10:00Z", offset=None)], fallback=0)
        self.assertEqual(parse(doc).records["steps"][0]["local_date"], "2026-10-10")
        self.assertIsNone(parse(doc).records["steps"][0]["start_offset_min"])         # в базе остаётся «неизвестно»

    def test_missing_offset_without_fallback_is_a_problem(self):
        exc = problems_of(hs.export(steps=[hs.step(offset=None)]))
        self.assertIn("смещения часового пояса", str(exc))

    def test_sleep_date_is_wake_date(self):
        doc = hs.export(sleep_sessions=[hs.sleep(start="2026-10-10T19:00:00Z", end="2026-10-10T22:30:00Z", stages=[],
                                                  offset=180)])
        self.assertEqual(parse(doc).records["sleep"][0]["local_date"], "2026-10-11")


class FileLevelProblemTests(unittest.TestCase):
    def test_not_json_empty_and_wrong_encoding(self):
        for data, text in [(b"", "пустой"), (b"   \n", "пустой"), (b"{oops", "не JSON"), (b"\xff\xfe\x00", "UTF-8"),
                           (b"[1, 2]", "объект"), (b'"text"', "объект"), (b"null", "объект")]:
            with self.subTest(data=data):
                self.assertIn(text, str(problems_of(data)))

    def test_nan_infinity_and_duplicate_keys(self):
        self.assertIn("NaN", str(problems_of(b'{"format": NaN}')))
        self.assertIn("Infinity", str(problems_of(b'{"x": Infinity}')))
        self.assertIn("повторяющийся", str(problems_of(b'{"format": "a", "format": "b"}')))

    def test_size_limit(self):
        with mock.patch.object(hf, "MAX_FILE_BYTES", 100):
            self.assertIn("слишком большой", str(problems_of(hs.day_file())))

    def test_record_count_limits(self):
        with mock.patch.object(hf, "MAX_RECORDS_PER_TYPE", 2):
            self.assertIn("слишком много", str(problems_of(hs.day_file())))

    def test_header_problems(self):
        base = hs.day_file()
        cases = {
            "format": dict(format="other"),
            "version 2": dict(format_version=2),
            "version string": dict(format_version="1"),
            "version bool": dict(format_version=True),
            "phone missing": dict(phone_id=None),
            "phone empty": dict(phone_id="  "),
            "phone number": dict(phone_id=5),
            "sequence 0": dict(sequence=0),
            "sequence negative": dict(sequence=-1),
            "sequence text": dict(sequence="1"),
            "sequence float": dict(sequence=1.0),
            "sequence bool": dict(sequence=True),
            "exported_at bad": dict(exported_at="вчера"),
            "exported_at no Z": dict(exported_at="2026-10-10T17:20:00"),
            "fallback 841": dict(fallback_utc_offset_min=841),
            "fallback text": dict(fallback_utc_offset_min="180"),
            "unknown top-level": dict(extra_field=1),
        }
        for label, change in cases.items():
            with self.subTest(label):
                doc = copy.deepcopy(base)
                doc.update(change)
                exc = problems_of(doc)
                self.assertTrue(exc.problems)
        for field in ("format", "format_version", "phone_id", "sequence", "exported_at"):
            with self.subTest(missing=field):
                doc = copy.deepcopy(base)
                doc.pop(field)
                self.assertTrue(problems_of(doc).problems)

    def test_valid_header_is_kept_for_journal_even_when_records_are_bad(self):
        doc = hs.day_file(sequence=7)
        doc["steps"][0]["count"] = 0
        exc = problems_of(doc)
        self.assertEqual((exc.header["phone_id"], exc.header["sequence"], exc.header["exported_at"]),
                         ("phone-01", 7, hs.exported_at_for(7)))
        self.assertEqual(exc.rejected, {"steps": 1, "heart_rate": 0, "sleep": 0})

    def test_bad_header_gives_no_journal_header(self):
        doc = hs.day_file()
        doc["sequence"] = 0
        self.assertIsNone(problems_of(doc).header)

    def test_sources(self):
        for label, sources in {
            "duplicate id": [hs.source("s1"), hs.source("s1", package="other")],
            "no package": [{"id": "s1"}],
            "empty package": [hs.source(package="")],
            "unknown device field": [{"id": "s1", "package": "p", "device": {"color": "red"}}],
            "not object": ["s1"],
            "device not text": [{"id": "s1", "package": "p", "device": {"model": 5}}],
        }.items():
            with self.subTest(label):
                self.assertTrue(problems_of(hs.export(steps=[hs.step()], sources=sources)).problems)

    def test_source_key_is_built_from_package_and_device(self):
        parsed = parse(hs.export(steps=[hs.step()], sources=[hs.source(package="app.zepp", manufacturer="ExampleBrand",
                                                                       model="Watch 1", type_="WATCH")]))
        self.assertEqual(parsed.sources["s1"]["key"], "health_connect|app.zepp|ExampleBrand|Watch 1|WATCH")
        parsed = parse(hs.export(steps=[hs.step()], sources=[hs.source(package="app.zepp")]))
        self.assertEqual(parsed.sources["s1"]["key"], "health_connect|app.zepp|||")

    def test_message_limit_and_total(self):
        doc = hs.export(steps=[hs.step(hc=f"s{i}", count=0) for i in range(120)])
        exc = problems_of(doc)
        self.assertEqual(len(exc.problems), hf.MAX_PROBLEMS)
        self.assertEqual(exc.total, 120)
        self.assertIn("ещё проблем: 70", str(exc))
        self.assertEqual(exc.rejected["steps"], 120)

    def test_error_paths_point_at_the_record(self):
        doc = hs.day_file()
        doc["heart_rate"][1]["samples"][0]["bpm"] = 301
        exc = problems_of(doc)
        self.assertEqual(exc.problems[0][0], "heart_rate[1].samples[0].bpm")
        self.assertIn("301", exc.problems[0][1])


class StepTests(unittest.TestCase):
    def bad(self, **change):
        rec = hs.step()
        rec.update(change)
        return problems_of(hs.export(steps=[rec]))

    def test_required_fields(self):
        for field in ("hc_id", "source", "start", "end", "last_modified", "count"):
            with self.subTest(field=field):
                rec = hs.step()
                rec.pop(field)
                self.assertTrue(problems_of(hs.export(steps=[rec])).problems)

    def test_count_range_and_type(self):
        for value in (0, -5, 1000001, "5", 5.0, 5.5, True, None, [5]):
            with self.subTest(count=value):
                self.bad(count=value)
        for value in (1, 1000000):
            parse(hs.export(steps=[hs.step(count=value)]))

    def test_time_order_is_strict_for_steps(self):
        self.bad(end="2026-10-10T00:00:00Z")
        self.bad(end="2026-10-09T23:59:59Z")
        parse(hs.export(steps=[hs.step(end="2026-10-10T00:00:00.000000001Z")]))      # на одну наносекунду позже: можно
        self.bad(start="2026-10-10T00:00:00.000000002Z", end="2026-10-10T00:00:00.000000001Z")

    def test_offsets_and_client_fields(self):
        for change in ({"start_offset_min": 841}, {"start_offset_min": -841}, {"start_offset_min": "180"},
                       {"end_offset_min": 1.5}, {"client_record_version": -1}, {"client_record_version": "1"},
                       {"client_record_id": 7}, {"hc_id": ""}, {"hc_id": 5}, {"source": "nope"}, {"unknown": 1}):
            with self.subTest(change=change):
                self.bad(**change)
        rec = parse(hs.export(steps=[hs.step(client_record_id="c-1", client_record_version=3)])).records["steps"][0]
        self.assertEqual((rec["client_record_id"], rec["client_record_version"]), ("c-1", 3))

    def test_times_are_stored_canonical(self):
        rec = parse(hs.export(steps=[hs.step(start="2026-10-10T00:00:00.5Z", end="2026-10-10T00:10:00.123456789Z",
                                             modified="2026-10-10T17:00:00.25Z")])).records["steps"][0]
        self.assertEqual((rec["start"], rec["end"], rec["last_modified"]),
                         ("2026-10-10T00:00:00.500000000Z", "2026-10-10T00:10:00.123456789Z", "2026-10-10T17:00:00.250000000Z"))

    def test_duplicates_across_file(self):
        self.assertIn("больше одного раза", str(problems_of(hs.export(steps=[hs.step("a"), hs.step("a")]))))
        doc = hs.export(steps=[hs.step("a")], heart_rate=[hs.heart("a")])
        self.assertIn("больше одного раза", str(problems_of(doc)))
        doc = hs.export(steps=[hs.step("a")], deleted=["a"])
        self.assertIn("списке удалений", str(problems_of(doc)))


class HeartRateTests(unittest.TestCase):
    def test_samples_required_and_validated(self):
        rec = hs.heart()
        rec.pop("samples")
        self.assertTrue(problems_of(hs.export(heart_rate=[rec])).problems)
        for bpm in (0, 301, -1, 61.5, "61", True, None):
            with self.subTest(bpm=bpm):
                problems_of(hs.export(heart_rate=[hs.heart(samples=[{"time": "2026-10-10T19:10:00Z", "bpm": bpm}])]))
        for bpm in (1, 300):
            parse(hs.export(heart_rate=[hs.heart(samples=[{"time": "2026-10-10T19:10:00Z", "bpm": bpm}])]))

    def test_duplicate_sample_time_even_with_different_spelling(self):
        samples = [{"time": "2026-10-10T19:10:00Z", "bpm": 60}, {"time": "2026-10-10T19:10:00.000Z", "bpm": 61}]
        self.assertIn("уже есть", str(problems_of(hs.export(heart_rate=[hs.heart(samples=samples)]))))

    def test_samples_that_differ_by_a_nanosecond_are_distinct(self):
        samples = [{"time": "2026-10-10T19:10:00.000000001Z", "bpm": 60}, {"time": "2026-10-10T19:10:00.000000002Z", "bpm": 61}]
        rec = parse(hs.export(heart_rate=[hs.heart(samples=samples)])).records["heart_rate"][0]
        self.assertEqual(len(rec["samples"]), 2)

    def test_equal_start_and_end_allowed_and_reversed_not(self):
        parse(hs.export(heart_rate=[hs.heart(start="2026-10-10T19:10:00Z", end="2026-10-10T19:10:00Z", samples=[])]))
        problems_of(hs.export(heart_rate=[hs.heart(start="2026-10-10T19:10:00Z", end="2026-10-10T19:09:00Z", samples=[])]))

    def test_sample_outside_record_is_a_warning_not_an_error(self):
        samples = [{"time": "2026-10-10T20:00:00Z", "bpm": 60}]
        parsed = parse(hs.export(heart_rate=[hs.heart(samples=samples)]))
        self.assertTrue(parsed.warnings)

    def test_samples_are_sorted(self):
        samples = [{"time": "2026-10-10T19:10:30Z", "bpm": 61}, {"time": "2026-10-10T19:10:00Z", "bpm": 60}]
        rec = parse(hs.export(heart_rate=[hs.heart(samples=samples)])).records["heart_rate"][0]
        self.assertEqual([bpm for _, bpm in rec["samples"]], [60, 61])


class SleepTests(unittest.TestCase):
    def test_stage_problems(self):
        base = hs.SCREEN_STAGES
        cases = {
            "unknown name": [{"stage": "rapid", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:10:00Z"}],
            "end equals start": [{"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:02:00Z"}],
            "end before start": [{"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:00:00Z"}],
            "duplicate start": [dict(base[0]), dict(base[0], stage="deep")],
            "missing field": [{"stage": "light", "start": "2026-10-10T07:02:00Z"}],
            "unknown field": [dict(base[0], color="blue")],
            "not object": ["light"],
        }
        for label, stages in cases.items():
            with self.subTest(label):
                problems_of(hs.export(sleep_sessions=[hs.sleep(stages=stages)]))

    def test_all_known_stage_names(self):
        stages = [{"stage": name, "start": f"2026-10-10T07:{i:02d}:00Z", "end": f"2026-10-10T07:{i:02d}:30Z"}
                  for i, name in enumerate(hf.STAGES)]
        self.assertEqual(len(parse(hs.export(sleep_sessions=[hs.sleep(stages=stages)])).records["sleep"][0]["stages"]), 8)

    def test_stages_required_but_may_be_empty(self):
        rec = hs.sleep()
        rec.pop("stages")
        problems_of(hs.export(sleep_sessions=[rec]))
        self.assertEqual(parse(hs.export(sleep_sessions=[hs.sleep(stages=[])])).records["sleep"][0]["stages"], [])

    def test_overlapping_or_outside_stages_warn(self):
        stages = [{"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:30:00Z"},
                  {"stage": "deep", "start": "2026-10-10T07:20:00Z", "end": "2026-10-10T07:40:00Z"}]
        self.assertTrue(parse(hs.export(sleep_sessions=[hs.sleep(stages=stages)])).warnings)
        stages = [{"stage": "light", "start": "2026-10-10T06:00:00Z", "end": "2026-10-10T07:10:00Z"}]
        self.assertTrue(parse(hs.export(sleep_sessions=[hs.sleep(stages=stages)])).warnings)

    def test_equal_start_and_end_allowed_for_session(self):
        parse(hs.export(sleep_sessions=[hs.sleep(start="2026-10-10T07:02:00Z", end="2026-10-10T07:02:00Z", stages=[])]))

    def test_text_limits(self):
        problems_of(hs.export(sleep_sessions=[hs.sleep(title="x" * 5000)]))


class PayloadHashTests(unittest.TestCase):
    def rec(self, doc, kind):
        return parse(doc).records[kind][0]

    def test_hash_ignores_id_modified_time_and_spelling(self):
        a = self.rec(hs.export(steps=[hs.step("a", modified="2026-10-10T17:00:00Z")]), "steps")
        b = self.rec(hs.export(steps=[hs.step("b", start="2026-10-10T00:00:00.000Z", modified="2026-10-11T00:00:00Z")]), "steps")
        self.assertEqual(a["payload_hash"], b["payload_hash"])

    def test_hash_changes_with_any_content(self):
        base = self.rec(hs.export(steps=[hs.step()]), "steps")["payload_hash"]
        for change in ({"count": 512}, {"end": "2026-10-10T00:10:00.000000001Z"}, {"offset": 120},
                       {"client_record_id": "x"}, {"client_record_version": 2}):
            with self.subTest(change=change):
                other = self.rec(hs.export(steps=[hs.step(**change)]), "steps")["payload_hash"]
                self.assertNotEqual(base, other)

    def test_hash_is_stable_under_sample_and_stage_order(self):
        samples = [{"time": "2026-10-10T19:10:00Z", "bpm": 60}, {"time": "2026-10-10T19:10:30Z", "bpm": 61}]
        a = self.rec(hs.export(heart_rate=[hs.heart(samples=samples)]), "heart_rate")["payload_hash"]
        b = self.rec(hs.export(heart_rate=[hs.heart(samples=list(reversed(samples)))]), "heart_rate")["payload_hash"]
        self.assertEqual(a, b)
        changed = [dict(samples[0], bpm=62), samples[1]]
        c = self.rec(hs.export(heart_rate=[hs.heart(samples=changed)]), "heart_rate")["payload_hash"]
        self.assertNotEqual(a, c)
        s1 = self.rec(hs.export(sleep_sessions=[hs.sleep()]), "sleep")["payload_hash"]
        s2 = self.rec(hs.export(sleep_sessions=[hs.sleep(stages=list(reversed(hs.SCREEN_STAGES)))]), "sleep")["payload_hash"]
        self.assertEqual(s1, s2)
        altered = [dict(hs.SCREEN_STAGES[0], stage="deep")] + hs.SCREEN_STAGES[1:]
        self.assertNotEqual(s1, self.rec(hs.export(sleep_sessions=[hs.sleep(stages=altered)]), "sleep")["payload_hash"])

    def test_changed_fallback_changes_local_date_and_hash(self):
        rec = hs.step(offset=None, start="2026-10-10T22:00:00Z", end="2026-10-10T22:10:00Z")
        a = self.rec(hs.export(steps=[rec], fallback=0), "steps")
        b = self.rec(hs.export(steps=[rec], fallback=180), "steps")
        self.assertNotEqual((a["local_date"], a["payload_hash"]), (b["local_date"], b["payload_hash"]))


class OrderPeekTests(unittest.TestCase):
    def test_peek_order(self):
        self.assertEqual(hf.peek_order(hs.encode(hs.day_file(sequence=5, phone="p"))), ("p", 5))
        self.assertIsNone(hf.peek_order(b"{broken"))
        self.assertIsNone(hf.peek_order(hs.encode({"phone_id": "p", "sequence": "5"})))


if __name__ == "__main__":
    unittest.main()
