"""Тихі години (без залежностей від HA, легко тестується).

Налаштування людини (`quiet`):

    {
      "mode": "schedule" | "entity" | "either",
      "intervals": [{"days": [0..6], "from": "22:00", "to": "07:00"}],
      "entity": {"entity_id": "...", "op": "is|not|gt|lt", "value": "..."},
      "behavior": {"low": "skip", "normal": "defer", "high": "silent"},
    }

Дні: 0 = понеділок. Інтервал із from > to переходить через північ і належить
дню початку. from == to означає всю добу. Пріоритет critical проходить завжди.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

MODES = ("schedule", "entity", "either")
OPS = ("is", "not", "gt", "lt")
BEHAVIORS = ("skip", "silent", "defer", "pass")
DEFAULT_BEHAVIOR = {"low": "skip", "normal": "defer", "high": "silent"}
DIGEST_AFTER = 3  # скільки відкладених за категорію показувати окремо

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$")


def parse_time(v: Any) -> time | None:
    if not isinstance(v, str):
        return None
    m = _TIME_RE.match(v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def norm_quiet(v: Any) -> dict:
    """Нормалізує налаштування тихих годин; порожнє або зламане дає вимкнені."""
    v = v if isinstance(v, dict) else {}
    mode = v.get("mode") if v.get("mode") in MODES else "schedule"
    intervals: list[dict] = []
    for it in v.get("intervals") if isinstance(v.get("intervals"), list) else []:
        if not isinstance(it, dict):
            continue
        f, t = parse_time(it.get("from")), parse_time(it.get("to"))
        if f is None or t is None:
            continue
        raw_days = it.get("days")
        days = sorted(
            {d for d in raw_days if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}
            if isinstance(raw_days, list)
            else set()
        )
        if not days:
            days = list(range(7))
        intervals.append({"days": days, "from": f.strftime("%H:%M"), "to": t.strftime("%H:%M")})
    ent = v.get("entity") if isinstance(v.get("entity"), dict) else {}
    entity = None
    if isinstance(ent.get("entity_id"), str) and "." in ent["entity_id"]:
        entity = {
            "entity_id": ent["entity_id"],
            "op": ent.get("op") if ent.get("op") in OPS else "is",
            "value": str(ent.get("value", "")),
        }
    beh = dict(DEFAULT_BEHAVIOR)
    raw = v.get("behavior") if isinstance(v.get("behavior"), dict) else {}
    for p in DEFAULT_BEHAVIOR:
        if raw.get(p) in ("skip", "silent", "defer"):
            beh[p] = raw[p]
    off = bool(v.get("off", False))
    enabled = (bool(intervals) or entity is not None) and not off
    if mode == "schedule" and not intervals:
        enabled = False
    if mode == "entity" and entity is None:
        enabled = False
    return {
        "enabled": enabled,
        "off": off,
        "mode": mode,
        "intervals": intervals,
        "entity": entity,
        "behavior": beh,
    }


def entity_matches(state: str | None, cond: dict | None) -> bool:
    """Чи виконується умова щодо стану сутності. Невідомий стан не вважається тишею."""
    if not cond or state is None or state in ("unknown", "unavailable"):
        return False
    op, value = cond.get("op", "is"), str(cond.get("value", ""))
    if op == "is":
        return state == value
    if op == "not":
        return state != value
    try:
        s, t = float(state), float(value)
    except ValueError:
        return False
    return s > t if op == "gt" else s < t


def _bounds(it: dict) -> tuple[time, time]:
    return parse_time(it["from"]), parse_time(it["to"])  # type: ignore[return-value]


def _interval_end(it: dict, now: datetime) -> datetime | None:
    """Якщо now усередині інтервалу, повертає момент його кінця, інакше None."""
    f, t = _bounds(it)
    days = it["days"]
    today = now.date()
    now_t = now.time().replace(second=0, microsecond=0)
    if f == t:
        # уся доба: діє в обрані дні; кінець опівночі
        if now.weekday() in days:
            return datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=now.tzinfo)
        return None
    if f < t:
        if now.weekday() in days and f <= now_t < t:
            return datetime.combine(today, t, tzinfo=now.tzinfo)
        return None
    # через північ
    if now.weekday() in days and now_t >= f:
        return datetime.combine(today + timedelta(days=1), t, tzinfo=now.tzinfo)
    if (now.weekday() - 1) % 7 in days and now_t < t:
        return datetime.combine(today, t, tzinfo=now.tzinfo)
    return None


def schedule_end(cfg: dict, now: datetime) -> datetime | None:
    """Кінець поточних тихих годин за розкладом (з урахуванням суміжних інтервалів) або None."""
    end: datetime | None = None
    cur = now
    for _ in range(30):
        ends = [e for it in cfg["intervals"] if (e := _interval_end(it, cur)) is not None]
        if not ends:
            break
        end = max(ends)
        cur = end
    return end


def in_schedule(cfg: dict, now: datetime) -> bool:
    return any(_interval_end(it, now) is not None for it in cfg["intervals"])


def is_quiet(cfg: dict, now: datetime, entity_state: str | None = None) -> bool:
    """Чи зараз тихі години для цієї людини."""
    if not cfg.get("enabled"):
        return False
    by_schedule = in_schedule(cfg, now) if cfg["mode"] in ("schedule", "either") else False
    by_entity = (
        entity_matches(entity_state, cfg["entity"]) if cfg["mode"] in ("entity", "either") else False
    )
    return by_schedule or by_entity


def behavior_for(cfg: dict, priority: str) -> str:
    """Що робити з повідомленням під час тихих годин: pass, skip, silent або defer."""
    if priority == "critical":
        return "pass"
    return cfg.get("behavior", DEFAULT_BEHAVIOR).get(priority, DEFAULT_BEHAVIOR.get(priority, "defer"))


def plan_flush(items: list[dict], limit: int = DIGEST_AFTER) -> tuple[list[dict], dict[str, list[dict]]]:
    """Ділить відкладені повідомлення: до limit на категорію окремо, більше одним зведенням.

    items: {"category", "ts", ...}. Повертає (окремі за часом, {category: [усі для зведення]}).
    """
    by_cat: dict[str, list[dict]] = {}
    for it in sorted(items, key=lambda x: x.get("ts", 0)):
        by_cat.setdefault(it["category"], []).append(it)
    singles: list[dict] = []
    digests: dict[str, list[dict]] = {}
    for cat, lst in by_cat.items():
        if len(lst) <= limit:
            singles.extend(lst)
        else:
            digests[cat] = lst
    singles.sort(key=lambda x: x.get("ts", 0))
    return singles, digests
