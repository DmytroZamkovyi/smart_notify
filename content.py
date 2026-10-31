"""Стандартизований вміст сповіщення (без залежностей від HA, легко тестується).

Єдиний формат `data` для всіх каналів:

    image, url, actions, tag, priority, format, sticky, clear, expire, progress

Решта ключів вважається специфічною для каналу й передається далі як є.
"""
from __future__ import annotations

import re
from typing import Any

PRIORITIES = ("low", "normal", "high", "critical")
FORMATS = ("plain", "html", "markdown")

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024

STANDARD_KEYS = (
    "image",
    "url",
    "actions",
    "tag",
    "priority",
    "format",
    "sticky",
    "clear",
    "expire",
    "progress",
)

_PRIORITY_ALIASES = {
    "min": "low",
    "low": "low",
    "default": "normal",
    "normal": "normal",
    "medium": "normal",
    "high": "high",
    "urgent": "high",
    "max": "critical",
    "critical": "critical",
    "emergency": "critical",
}

_FORMAT_ALIASES = {
    "plain": "plain",
    "text": "plain",
    "plain_text": "plain",
    "html": "html",
    "markdown": "markdown",
    "md": "markdown",
    "markdownv2": "markdown",
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def norm_priority(v: Any) -> str:
    if isinstance(v, str):
        return _PRIORITY_ALIASES.get(v.strip().lower(), "normal")
    return "normal"


def norm_format(v: Any) -> str:
    if isinstance(v, str):
        return _FORMAT_ALIASES.get(v.strip().lower(), "plain")
    return "plain"


def parse_duration(v: Any) -> float | None:
    """Секунди з числа або рядка: 90, "90", "30m", "2h", "1d". Некоректне або <=0 -> None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        m = _DURATION_RE.match(v)
        if m:
            sec = float(m.group(1)) * _UNITS[m.group(2).lower()]
            return sec if sec > 0 else None
    return None


def parse_progress(v: Any) -> dict | None:
    """Прогрес: число 0..100, або {"value": x, "max": y}. Повертає {"value","max","percent"}."""
    if isinstance(v, bool) or v is None:
        return None
    value: float | None = None
    mx: float = 100.0
    if isinstance(v, (int, float)):
        value = float(v)
    elif isinstance(v, dict):
        try:
            value = float(v.get("value"))
            mx = float(v.get("max", 100))
        except (TypeError, ValueError):
            return None
    elif isinstance(v, str):
        try:
            value = float(v.strip().rstrip("%"))
        except ValueError:
            return None
    if value is None or mx <= 0:
        return None
    value = min(max(value, 0.0), mx)
    return {"value": value, "max": mx, "percent": int(round(value / mx * 100))}


def parse_actions(v: Any) -> list[dict]:
    """Кнопки. Елемент: {"id","title"[,"url"]}; також приймаються ключі action/uri.

    Кнопка з url відкриває посилання, без url надсилає подію з id.
    """
    out: list[dict] = []
    for item in v if isinstance(v, list) else []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        aid = str(item.get("id") or item.get("action") or "").strip()
        url = str(item.get("url") or item.get("uri") or "").strip()
        if not title or not (aid or url):
            continue
        entry = {"title": title}
        if url:
            entry["url"] = url
            if aid:
                entry["id"] = aid
        else:
            entry["id"] = aid
        out.append(entry)
    return out


def parse_data(data: Any) -> tuple[dict, dict]:
    """Розділяє `data` на стандартну частину й решту (специфічні ключі каналів).

    Повертає (std, extra). std завжди має priority, format, sticky, clear.
    """
    data = data if isinstance(data, dict) else {}
    std: dict[str, Any] = {
        "priority": norm_priority(data.get("priority")),
        "format": norm_format(data.get("format")),
        "sticky": bool(data.get("sticky", False)),
        "clear": bool(data.get("clear", False)),
    }
    image = data.get("image")
    if isinstance(image, str) and image.strip():
        std["image"] = image.strip()
    elif isinstance(image, dict) and image.get("camera"):
        # {"camera": "camera.gate"}: знімок камери
        std["image"] = {"camera": str(image["camera"])}
    url = data.get("url")
    if isinstance(url, str) and url.strip():
        std["url"] = url.strip()
    tag = data.get("tag")
    if isinstance(tag, str) and tag.strip():
        std["tag"] = tag.strip()
    actions = parse_actions(data.get("actions"))
    if actions:
        std["actions"] = actions
    expire = parse_duration(data.get("expire"))
    if expire:
        std["expire"] = expire
    progress = parse_progress(data.get("progress"))
    if progress:
        std["progress"] = progress
    extra = {k: v for k, v in data.items() if k not in STANDARD_KEYS}
    return std, extra


def is_camera_image(image: Any) -> bool:
    return isinstance(image, dict) and "camera" in image


def classify_image(image: str) -> str:
    """url (http/https), local (/local/...), media (/media/...), path (абсолютний файл) або other."""
    s = image.strip()
    if re.match(r"^https?://", s, re.IGNORECASE):
        return "url"
    if s.startswith("/local/") or s.startswith("local/"):
        return "local"
    if s.startswith("/media/") or s.startswith("media/"):
        return "media"
    if s.startswith("/"):
        return "path"
    return "other"


def _cut(text: str, limit: int) -> tuple[str, str]:
    """Відрізає від початку text шматок не довший за limit: по абзацу, рядку, слову або жорстко."""
    if len(text) <= limit:
        return text, ""
    window = text[:limit]
    cut = -1
    # розрив бажано в другій половині вікна, щоб не лишати крихітний перший шматок
    for sep in ("\n\n", "\n", " "):
        i = window.rfind(sep)
        if i >= limit // 2:
            cut = i + len(sep)
            break
    if cut <= 0:
        i = window.rfind(" ")
        cut = i + 1 if i > 0 else limit
    chunk = text[:cut].rstrip()
    if not chunk:
        chunk = text[:cut]
    return chunk, text[cut:].lstrip("\n ")


def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Ділить текст на частини не довші за limit."""
    if limit < 1:
        raise ValueError("limit must be positive")
    rest = text or ""
    parts: list[str] = []
    while True:
        chunk, rest = _cut(rest, limit)
        parts.append(chunk)
        if not rest:
            return parts


def telegram_texts(title: str, message: str, has_photo: bool) -> tuple[str, list[str]]:
    """Тексти для Telegram: (головний текст/підпис, решта окремими повідомленнями).

    Без фото ліміт 4096; з фото підпис до 1024, а залишок іде окремими
    повідомленнями за лімітом 4096.
    """
    full = f"{title}\n{message}" if title and message else (title or message or "")
    first, rest = _cut(full, TG_CAPTION_LIMIT if has_photo else TG_TEXT_LIMIT)
    return first, (split_text(rest, TG_TEXT_LIMIT) if rest else [])


def progress_bar(percent: int | float, width: int = 10) -> str:
    """Текстова смуга прогресу: ▓▓▓▓░░░░░░ 40%."""
    p = min(max(int(round(percent)), 0), 100)
    filled = int(round(width * p / 100))
    return f"{'▓' * filled}{'░' * (width - filled)} {p}%"


def with_progress(message: str, progress: dict | None) -> str:
    if not progress:
        return message
    bar = progress_bar(progress["percent"])
    return f"{message}\n{bar}" if message else bar


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def expired_text(text: str, format_: str = "plain") -> str:
    """Додає позначку «НЕАКТУАЛЬНО» на початок тексту (повідомлення не видаляється)."""
    mark = "НЕАКТУАЛЬНО"
    if format_ == "html":
        return f"<b>{mark}</b>\n{text}"
    return f"{mark}\n{text}"
