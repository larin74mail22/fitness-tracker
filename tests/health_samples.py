"""Построители синтетических файлов выгрузки Health Connect (формат v1) для тестов. Все данные выдуманные."""

import json
from pathlib import Path

DEFAULT_MODIFIED = "2026-10-10T17:00:00Z"

# Синтетические стадии сна (выдуманные).
SCREEN_STAGES = [
    {"stage": "light", "start": "2026-10-10T07:02:00Z", "end": "2026-10-10T07:30:00Z"},
    {"stage": "deep", "start": "2026-10-10T07:30:00Z", "end": "2026-10-10T07:50:00Z"},
    {"stage": "light", "start": "2026-10-10T07:50:00Z", "end": "2026-10-10T08:10:00Z"},
    {"stage": "awake", "start": "2026-10-10T08:10:00Z", "end": "2026-10-10T08:12:00Z"},
    {"stage": "light", "start": "2026-10-10T08:12:00Z", "end": "2026-10-10T08:40:00Z"},
    {"stage": "awake", "start": "2026-10-10T08:40:00Z", "end": "2026-10-10T08:43:00Z"},
    {"stage": "light", "start": "2026-10-10T08:43:00Z", "end": "2026-10-10T09:15:00Z"},
    {"stage": "awake", "start": "2026-10-10T09:15:00Z", "end": "2026-10-10T09:20:00Z"},
]


def source(sid="s1", package="app.example.zepp", manufacturer=None, model=None, type_=None):
    return {"id": sid, "package": package, "device": {"manufacturer": manufacturer, "model": model, "type": type_}}


def step(hc="step-1", start="2026-10-10T00:00:00Z", end="2026-10-10T00:10:00Z", count=420, src="s1",
         modified=DEFAULT_MODIFIED, offset=180, **extra):
    rec = {"hc_id": hc, "source": src, "start": start, "end": end, "start_offset_min": offset,
           "end_offset_min": offset, "count": count, "last_modified": modified}
    rec.update(extra)
    return rec


def heart(hc="hr-1", start="2026-10-10T19:10:00Z", end="2026-10-10T19:11:00Z", bpms=(61,), src="s1",
          modified=DEFAULT_MODIFIED, offset=180, samples=None, **extra):
    if samples is None:
        samples = [{"time": start, "bpm": b} if i == 0 else {"time": f"2026-10-10T19:1{i}:30Z", "bpm": b}
                   for i, b in enumerate(bpms)]
    rec = {"hc_id": hc, "source": src, "start": start, "end": end, "start_offset_min": offset,
           "end_offset_min": offset, "samples": samples, "last_modified": modified}
    rec.update(extra)
    return rec


def sleep(hc="sleep-1", start="2026-10-10T07:02:00Z", end="2026-10-10T15:20:00Z", stages=None, src="s1",
          modified=DEFAULT_MODIFIED, offset=180, title=None, notes=None, **extra):
    rec = {"hc_id": hc, "source": src, "start": start, "end": end, "start_offset_min": offset,
           "end_offset_min": offset, "title": title, "notes": notes,
           "stages": [dict(s) if isinstance(s, dict) else s for s in (SCREEN_STAGES if stages is None else stages)], "last_modified": modified}
    rec.update(extra)
    return rec


def exported_at_for(sequence):
    """Время экспорта растёт вместе с номером файла: №1 = 17:20, №2 = 17:21 и т. д. (ограничено, чтобы не выйти за сутки)."""
    return f"2026-10-10T17:{20 + min(sequence - 1, 39):02d}:00Z"


def export(sequence=1, steps=(), heart_rate=(), sleep_sessions=(), deleted=(), phone="phone-01", sources=None,
           fallback=None, exported_at=None, **extra):
    exported_at = exported_at or exported_at_for(sequence)
    doc = {"format": "fitness-health-export", "format_version": 1, "phone_id": phone, "sequence": sequence,
           "exported_at": exported_at, "sources": sources if sources is not None else [source()],
           "steps": list(steps), "heart_rate": list(heart_rate), "sleep": list(sleep_sessions), "deleted": list(deleted)}
    if fallback is not None:
        doc["fallback_utc_offset_min"] = fallback
    doc.update(extra)
    return doc


def day_file(sequence=1, phone="phone-01", **overrides):
    """Один синтетический «день»: шаги окнами по 10 минут, пульс по минуте, сон 07:02–15:20."""
    steps = [step("step-1", "2026-10-10T00:00:00Z", "2026-10-10T00:10:00Z", 420),
             step("step-2", "2026-10-10T00:10:00Z", "2026-10-10T00:20:00Z", 730),
             step("step-3", "2026-10-10T16:30:00Z", "2026-10-10T16:40:00Z", 9)]
    hearts = [heart("hr-1", "2026-10-10T19:10:00Z", "2026-10-10T19:11:00Z", (61,)),
              heart("hr-2", "2026-10-10T19:05:00Z", "2026-10-10T19:06:00Z", (64,)),
              heart("hr-3", "2026-10-10T19:00:00Z", "2026-10-10T19:01:00Z", (78,))]
    doc = export(sequence, steps, hearts, [sleep("sleep-1")], phone=phone)
    doc.update(overrides)
    return doc


def encode(doc):
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def write(directory, name, doc):
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(doc) if not isinstance(doc, bytes) else doc)
    return path
