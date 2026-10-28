"""Разбор того, что админ пишет руками: время публикации и таймкод отрывка трека."""

import re
from datetime import datetime, timedelta

# ---------- время публикации ----------

_REL_RE = re.compile(r"^\+\s*(\d+)\s*(м|мин|минут\w*|m|min|ч|час\w*|h)$")
_TIME_RE = re.compile(r"^(?:(сегодня|завтра|послезавтра)\s+)?(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\s+(\d{1,2}):(\d{2})$")
_DAY_SHIFT = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def parse_when(text: str, now: datetime) -> datetime | None:
    """Разбирает время публикации. Возвращает None, если формат непонятен."""
    text = text.strip().lower()
    try:
        if m := _REL_RE.match(text):
            amount = int(m[1])
            minutes = amount if m[2][0] in "мm" else amount * 60
            return now + timedelta(minutes=minutes)

        if m := _TIME_RE.match(text):
            day = now + timedelta(days=_DAY_SHIFT.get(m[1] or "сегодня", 0))
            when = day.replace(hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0)
            if not m[1] and when <= now:
                when += timedelta(days=1)
            return when

        if m := _DATE_RE.match(text):
            year = int(m[3]) if m[3] else now.year
            if year < 100:
                year += 2000
            when = now.replace(
                year=year, month=int(m[2]), day=int(m[1]),
                hour=int(m[4]), minute=int(m[5]), second=0, microsecond=0,
            )
            if not m[3] and when <= now:
                when = when.replace(year=year + 1)
            return when
    except ValueError:  # 31.02, 25:00 и т. п.
        return None
    return None


def quick_time(code: str, now: datetime) -> datetime:
    """Время с кнопки: «h3» — через 3 часа, «t10» — завтра в 10:00."""
    if code.startswith("h"):
        return now + timedelta(hours=int(code[1:]))
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=int(code[1:]), minute=0, second=0, microsecond=0)


# ---------- таймкод отрывка ----------

MUSIC_DEFAULT_LEN = 30   # сек, если указано только начало
MUSIC_MAX_LEN = 300
_TC_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$|^(\d+)$")


def _seconds(text: str) -> int | None:
    m = _TC_RE.match(text.strip())
    if not m:
        return None
    if m[4]:
        return int(m[4])
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])


def parse_timecode(text: str) -> tuple[int, int] | None:
    """«1:05» → (65, 30); «1:05-1:40» → (65, 35). None — формат не понят."""
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if len(parts) > 2:
        return None
    start = _seconds(parts[0])
    if start is None:
        return None
    if len(parts) == 1:
        return start, MUSIC_DEFAULT_LEN
    end = _seconds(parts[1])
    if end is None or end <= start:
        return None
    return start, min(end - start, MUSIC_MAX_LEN)
