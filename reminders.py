"""Нагадування до події й пропуски в тексті (без залежностей від HA, легко тестується).

Пропуски в title/message заповнюються в момент надсилання, окремо для кожного
отримувача:

    {початок} {початок:через} {початок:хв} {початок:%d.%m %H:%M}
    {<ключ з vars>} {<ключ>:через} {<ключ>:хв}
    {стан:sensor.x}
    {людина}

Англійські синоніми: start/event, in, min, state, person.
Невідомі пропуски й інші фігурні дужки лишаються як є.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

EVENT_NAMES = ("початок", "start", "подія", "event")
STATE_NAMES = ("стан", "state")
PERSON_NAMES = ("людина", "person")
REL_FORMATS = ("через", "in")
MIN_FORMATS = ("хв", "min")

LEAD_OFF = "off"
LEAD_MAX = 7 * 24 * 60  # хвилин
FIRE_GRACE = 300  # секунд: скільки після події ще надсилати запізніле нагадування

_EMPTY = ("", "unknown", "unavailable", "none", "null")
_ISO_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_PH_RE = re.compile(r"\{([^{}:\s]+)(?::([^{}]+))?\}")
_KEY_RE = re.compile(r"^\w+$")


# ------------------------------------------------------------------ час події


def parse_event_time(value: Any, tz: tzinfo) -> datetime | None:
    """Час події з рядка ISO 8601, datetime або Unix timestamp.

    Порожнє значення (а також unknown/unavailable/none) дає None.
    Без часового поясу береться tz. Некоректне значення: ValueError.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    s = str(value).strip()
    if s.lower() in _EMPTY:
        return None
    try:
        return datetime.fromtimestamp(float(s), timezone.utc)
    except ValueError:
        pass
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def as_time(value: Any, tz: tzinfo) -> datetime | None:
    """Значення з vars як час, якщо воно схоже на дату й час; інакше None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, str) and _ISO_RE.match(value):
        try:
            return parse_event_time(value, tz)
        except ValueError:
            return None
    return None


def norm_vars(raw: Any) -> dict[str, Any]:
    """Словник vars, придатний для збереження: ключі-слова, значення рядки або числа."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        k = str(k).strip()
        if not _KEY_RE.match(k) or k.lower() in EVENT_NAMES:
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        elif not isinstance(v, (str, int, float, bool)) and v is not None:
            v = str(v)
        out[k] = v
    return out


# ------------------------------------------------------------------ «за скільки»


def norm_lead(v: Any) -> int | str | None:
    """Налаштування «Нагадувати за»: None (не налаштовано), хвилини або "off"."""
    if v is None or isinstance(v, bool):
        return None
    if v == LEAD_OFF:
        return LEAD_OFF
    if isinstance(v, dict):  # {"days","hours","minutes"} із селектора тривалості
        try:
            mins = (
                float(v.get("days", 0) or 0) * 1440
                + float(v.get("hours", 0) or 0) * 60
                + float(v.get("minutes", 0) or 0)
            )
        except (TypeError, ValueError):
            return None
        return min(max(int(round(mins)), 0), LEAD_MAX)
    try:
        mins = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return min(max(mins, 0), LEAD_MAX)


def norm_leads(raw: Any) -> dict[str, int | str]:
    """id категорії -> налаштування (не налаштовані не зберігаються)."""
    out: dict[str, int | str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            n = norm_lead(v)
            if n is not None:
                out[str(k)] = n
    return out


def due_of(event: datetime, lead: int | None) -> datetime:
    """Коли надсилати заплановану частину: подія мінус «за скільки» (не налаштовано = у момент події)."""
    return event - timedelta(minutes=lead or 0)


def fingerprint(event: datetime, title: str | None, message: str, variables: dict) -> str:
    """Відбиток нагадування: однаковий час події, текст і vars дають однаковий відбиток."""
    raw = json.dumps(
        [event.timestamp(), title or "", message or "", variables],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha1(raw.encode()).hexdigest()


# ------------------------------------------------------------------ пропуски


def humanize(delta: float) -> str:
    """«через 30 хв», «через 1 год 15 хв», «через 2 дн 3 год», «5 хв тому», «зараз»."""
    if abs(delta) < 60:
        return "зараз"
    total = int(round(abs(delta) / 60))
    days, rest = divmod(total, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        text = f"{days} дн" + (f" {hours} год" if hours else "")
    elif hours:
        text = f"{hours} год" + (f" {mins} хв" if mins else "")
    else:
        text = f"{mins} хв"
    return f"через {text}" if delta > 0 else f"{text} тому"


def _fmt_time(t: datetime, fmt: str | None, now: datetime, tz: tzinfo) -> str | None:
    delta = (t - now).total_seconds()
    if fmt is None:
        local = t.astimezone(tz)
        if local.date() == now.astimezone(tz).date():
            return local.strftime("%H:%M")
        return local.strftime("%d.%m %H:%M")
    low = fmt.strip().lower()
    if low in REL_FORMATS:
        return humanize(delta)
    if low in MIN_FORMATS:
        return str(int(round(delta / 60)))
    if "%" in fmt:
        return t.astimezone(tz).strftime(fmt)
    return None


def has_placeholders(text: str | None) -> bool:
    return bool(text) and _PH_RE.search(text) is not None


def render(
    text: str | None,
    *,
    now: datetime,
    tz: tzinfo,
    event: datetime | None = None,
    variables: dict | None = None,
    person: str = "",
    state_of: Callable[[str], str | None] | None = None,
    html: bool = False,
) -> str | None:
    """Заповнює пропуски. Нерозпізнане лишається без змін."""
    if not text or "{" not in text:
        return text
    variables = variables or {}
    lowered = {str(k).lower(): v for k, v in variables.items()}

    def sub(m: re.Match) -> str:
        name, fmt = m.group(1), m.group(2)
        low = name.lower()
        out: str | None = None
        if low in EVENT_NAMES:
            out = _fmt_time(event, fmt, now, tz) if event else None
        elif low in STATE_NAMES:
            ent = (fmt or "").strip()
            if ent and state_of is not None:
                val = state_of(ent)
                out = val if val is not None else "—"
        elif low in PERSON_NAMES and fmt is None:
            out = person
        elif low in lowered:
            raw = lowered[low]
            t = as_time(raw, tz)
            if t is not None:
                out = _fmt_time(t, fmt, now, tz)
            elif fmt is None:
                out = "" if raw is None else str(raw)
        if out is None:
            return m.group(0)
        return out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if html else out

    return _PH_RE.sub(sub, text)


# ------------------------------------------------ пошук викликів у конфігурації


def _is_smart(action: Any) -> bool:
    return isinstance(action, str) and action.strip().lower() == "notify.smart"


def _has_event_time(d: dict) -> bool:
    for key in ("data", "data_template"):
        data = d.get(key)
        if isinstance(data, dict):
            v = data.get("event_time")
            if v not in (None, "") and not (isinstance(v, str) and not v.strip()):
                return True
    return False


def uses_event_time(config: Any) -> bool:
    """Чи є в конфігурації автоматизації/скрипта виклик notify.smart з полем event_time."""
    stack = [config]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if (_is_smart(node.get("action")) or _is_smart(node.get("service"))) and _has_event_time(node):
                return True
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False
