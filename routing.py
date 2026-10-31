"""Чиста логіка категорій і маршрутів (без залежностей від HA, легко тестується)."""
from __future__ import annotations

from collections.abc import Iterable

MODES = ("inherit", "off", "send")

DEFAULT_GENERAL = "general"  # типове адміна: як у загальному налаштуванні користувача
DEFAULT_OFF = "off"  # типове адміна: вимкнено для всіх
DEFAULTS = (DEFAULT_GENERAL, DEFAULT_OFF)

ERR_UNKNOWN = "unknown_category"
ERR_GROUP = "category_is_group"

OFF_SETTING = {"mode": "off", "channels": [], "fallback": []}


def norm_setting(v, allowed: set[str] | None = None) -> dict:
    """Нормалізує налаштування маршруту.

    Новий формат: {"mode": inherit|off|send, "channels": [...], "fallback": [...]}.
    Старий формат (рядок: "default", "off" або id каналу) теж приймається.
    allowed: якщо задано, лишає лише ці id каналів.
    """
    if isinstance(v, dict):
        mode = v.get("mode", "inherit")
        channels = v.get("channels", [])
        fallback = v.get("fallback", [])
    elif v == "off":
        mode, channels, fallback = "off", [], []
    elif v in (None, "", "default"):
        mode, channels, fallback = "inherit", [], []
    else:
        mode, channels, fallback = "send", [v], []

    if mode not in MODES:
        mode = "inherit"

    def clean(items, banned=()) -> list[str]:
        out: list[str] = []
        for x in items if isinstance(items, list) else []:
            if not isinstance(x, str) or x in out or x in banned:
                continue
            if allowed is not None and x not in allowed:
                continue
            out.append(x)
        return out

    channels = clean(channels)
    fallback = clean(fallback, banned=channels)
    if mode == "send" and not channels and not fallback:
        mode = "inherit"
    if mode != "send":
        channels, fallback = [], []
    return {"mode": mode, "channels": channels, "fallback": fallback}


def _minutes(v) -> int:
    """Невід'ємне ціле число хвилин; некоректне дає 0."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return max(n, 0)


def norm_category(data: dict) -> dict:
    """Доповнює дані категорії типовими значеннями (старі записи без нових полів)."""
    parent = data.get("parent")
    default = data.get("default")
    return {
        **data,
        "parent": parent if isinstance(parent, str) and parent else None,
        "prefix": str(data.get("prefix") or "").strip(),
        "default": default if default in DEFAULTS else DEFAULT_GENERAL,
        "service": bool(data.get("service", False)),
        "icon": str(data.get("icon") or "").strip(),
        "dedup": _minutes(data.get("dedup")),
    }


def category_map(raw: Iterable[dict]) -> dict[str, dict]:
    """id -> нормалізована категорія.

    Некоректний батько (немає такої категорії, сам себе, сам є підкатегорією)
    ігнорується: така категорія вважається звичайною (верхнього рівня).
    """
    out = {}
    for c in raw:
        n = norm_category(c)
        out[n["id"]] = n
    raw_parent = {cid: c["parent"] for cid, c in out.items()}
    for cid, c in out.items():
        p = c["parent"]
        if p is not None and (p == cid or p not in raw_parent or raw_parent[p] is not None):
            c["parent"] = None
    return out


def group_ids(cats: dict[str, dict]) -> set[str]:
    """Категорії, що мають підкатегорії (лише групові налаштування)."""
    return {c["parent"] for c in cats.values() if c["parent"]}


def children_of(cats: dict[str, dict], cid: str) -> list[str]:
    return sorted(
        (i for i, c in cats.items() if c["parent"] == cid),
        key=lambda i: cats[i]["name"].lower(),
    )


def ordered_ids(cats: dict[str, dict]) -> list[str]:
    """Дерево: категорії верхнього рівня за назвою, під кожною її підкатегорії."""
    top = sorted(
        (i for i, c in cats.items() if c["parent"] is None),
        key=lambda i: cats[i]["name"].lower(),
    )
    out: list[str] = []
    for i in top:
        out.append(i)
        out.extend(children_of(cats, i))
    return out


def label(cats: dict[str, dict], cid: str) -> str:
    """«Батько › Назва» для підкатегорії, інакше «Назва»."""
    c = cats[cid]
    if c["parent"]:
        return f"{cats[c['parent']]['name']} › {c['name']}"
    return c["name"]


def check_callable(cats: dict[str, dict], cid: str) -> str | None:
    """Код помилки, якщо на категорію не можна надсилати; None, якщо можна."""
    if cid not in cats:
        return ERR_UNKNOWN
    if cid in group_ids(cats):
        return ERR_GROUP
    return None


def chain(cats: dict[str, dict], cid: str) -> list[str]:
    """Ланцюжок для розв'язання налаштувань: підкатегорія, потім її батько."""
    parent = cats[cid]["parent"]
    return [cid, parent] if parent else [cid]


def admin_off(cats: dict[str, dict], cid: str) -> bool:
    """Чи вимкнена категорія (або її батько) типовим налаштуванням адміна."""
    return any(cats[i]["default"] == DEFAULT_OFF for i in chain(cats, cid))


def resolve_setting(cats: dict[str, dict], cid: str, route: dict | None) -> dict:
    """Ефективне налаштування користувача для категорії.

    1. Власні налаштування користувача: підкатегорія, потім категорія
       (перше, де режим не inherit).
    2. Типове адміна: якщо підкатегорія або категорія вимкнена за замовчуванням, тиша.
    3. Загальне налаштування користувача.
    """
    route = route if isinstance(route, dict) else {}
    own = route.get("cats", {})
    own = own if isinstance(own, dict) else {}
    for i in chain(cats, cid):
        s = norm_setting(own.get(i))
        if s["mode"] != "inherit":
            return s
    if admin_off(cats, cid):
        return dict(OFF_SETTING)
    return norm_setting(route.get("default"))


def prefix_of(cats: dict[str, dict], cid: str) -> str:
    """Префікс у дужках: префікс батька, потім власний, наприклад «[Світло][Екстрені]»."""
    parts = []
    for i in reversed(chain(cats, cid)):
        p = cats[i]["prefix"]
        if p:
            parts.append(f"[{p}]")
    return "".join(parts)


def apply_prefix(prefix: str, payload: dict) -> dict:
    """Префікс іде в заголовок. Без заголовка заголовком стає сам префікс."""
    if not prefix:
        return payload
    out = dict(payload)
    title = out.get("title")
    out["title"] = f"{prefix} {title}" if title else prefix
    return out


def dedup_minutes(cats: dict[str, dict], cid: str) -> int:
    """Вікно усунення дублікатів у хвилинах: підкатегорія, потім категорія; 0 це вимкнено."""
    for i in chain(cats, cid):
        if cats[i]["dedup"] > 0:
            return cats[i]["dedup"]
    return 0


def icon_of(cats: dict[str, dict], cid: str) -> str:
    """Значок: власний, інакше батька, інакше порожньо."""
    for i in chain(cats, cid):
        if cats[i]["icon"]:
            return cats[i]["icon"]
    return ""
