"""Доставка в канали: телефон, Telegram, браузер, інше (залежить від HA).

Кожна `deliver_*` приймає підготовлене повідомлення `Msg` і повертає список
посилань (refs) на те, що було відправлено: вони потрібні, щоб пізніше
прибрати сповіщення (expire) або відредагувати його (прогрес у Telegram).
"""
from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from . import content
from .const import FALLBACK_SOURCE

_LOGGER = logging.getLogger(__name__)

# Ключі data, які в Telegram передаються в telegram_bot як є
TELEGRAM_PASSTHROUGH = (
    "disable_notification",
    "disable_web_page_preview",
    "keyboard",
    "message_tag",
    "reply_to_message_id",
    "message_thread_id",
)

# Ключі data, які в html5.send_message передаються як є
HTML5_PASSTHROUGH = (
    "icon",
    "badge",
    "dir",
    "renotify",
    "silent",
    "require_interaction",
    "vibrate",
    "lang",
    "timestamp",
    "ttl",
    "urgency",
    "data",
)

PARSE_MODES = {"html": "html", "markdown": "markdown", "plain": "plain_text"}
TMP_DIR = ".smart_notify_tmp"


@dataclass
class Msg:
    """Повідомлення, підготовлене до доставки."""

    id: str
    message: str
    title: str | None
    std: dict
    extra: dict
    raw: dict
    category: str
    cat_name: str = ""
    icon: str = ""
    silent: bool = False
    user: str = ""
    note: str = ""  # наприклад, «02:15» для відкладених
    refs: list = field(default_factory=list)

    @property
    def tag(self) -> str | None:
        return self.std.get("tag")

    @property
    def priority(self) -> str:
        return self.std.get("priority", "normal")

    @property
    def body(self) -> str:
        return f"{self.message}\n({self.note})" if self.note else self.message


# ---------------------------------------------------------------- допоміжне


def _entity_platform(hass: HomeAssistant, entity_id: str) -> str | None:
    ent = er.async_get(hass).async_get(entity_id)
    return ent.platform if ent else None


async def _call(
    hass: HomeAssistant, domain: str, service: str, data: dict, response: bool = False
) -> Any:
    """Виклик дії; якщо дія не підтримує відповідь, викликаємо без неї."""
    if response:
        try:
            return await hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )
        except ServiceValidationError as err:
            if "response" not in str(err).lower():
                raise
    await hass.services.async_call(domain, service, data, blocking=True)
    return None


def _media_dirs(hass: HomeAssistant) -> dict[str, str]:
    return dict(getattr(hass.config, "media_dirs", {}) or {})


def resolve_image(hass: HomeAssistant, image: Any) -> dict:
    """Повертає {"url", "path", "camera"}: що відомо про картинку.

    url: адреса для телефона й браузера (відносна чи повна); path: файл на диску
    для Telegram; camera: сутність камери, якщо потрібен знімок.
    """
    if isinstance(image, dict) and image.get("camera"):
        cam = image["camera"]
        return {"url": f"/api/camera_proxy/{cam}", "path": None, "camera": cam}
    s = str(image).strip()
    kind = content.classify_image(s)
    if kind == "url":
        return {"url": s, "path": None, "camera": None}
    if not s.startswith("/"):
        s = "/" + s
    if s.startswith("/local/"):
        rel = s[len("/local/") :]
        return {"url": s, "path": hass.config.path("www", rel), "camera": None}
    dirs = _media_dirs(hass)
    if s.startswith("/media/"):
        name, _, rel = s[len("/media/") :].partition("/")
        if name in dirs and rel:  # адреса media-source: /media/<джерело>/<файл>
            return {"url": s, "path": os.path.join(dirs[name], rel), "camera": None}
    # шлях у файловій системі: якщо він у www чи в медіа-папці, є й адреса
    for name, base in dirs.items():
        base = base.rstrip("/")
        if s.startswith(base + "/"):
            return {"url": f"/media/{name}/{s[len(base) + 1:]}", "path": s, "camera": None}
    www = hass.config.path("www").rstrip("/")
    if s.startswith(www + "/"):
        return {"url": f"/local/{s[len(www) + 1:]}", "path": s, "camera": None}
    return {"url": None, "path": s, "camera": None}


async def _camera_to_file(hass: HomeAssistant, entity_id: str) -> str:
    """Знімок камери у тимчасовий файл у медіа-папці (поза публічним www)."""
    from homeassistant.components.camera import async_get_image

    dirs = list(_media_dirs(hass).values())
    if not dirs:
        raise HomeAssistantError(
            "Для знімка камери в Telegram потрібна медіа-папка HA (media_dirs)"
        )
    img = await async_get_image(hass, entity_id)
    folder = Path(dirs[0]) / TMP_DIR
    name = f"{entity_id.replace('.', '_')}_{int(_time.time() * 1000)}.jpg"
    path = folder / name

    def _write() -> str:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img.content)
        return str(path)

    return await hass.async_add_executor_job(_write)


async def _remove_file(hass: HomeAssistant, path: str) -> None:
    def _rm() -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    await hass.async_add_executor_job(_rm)


# ------------------------------------------------------------------ телефон


PRIORITY_ANDROID = {
    "low": {"importance": "low"},
    "normal": {},
    "high": {"importance": "high", "priority": "high", "ttl": 0},
    "critical": {"importance": "max", "priority": "high", "ttl": 0},
}
PRIORITY_IOS = {
    "low": "passive",
    "normal": "active",
    "high": "time-sensitive",
    "critical": "critical",
}


def phone_payload(hass: HomeAssistant, m: Msg) -> dict:
    """Стандартний data -> ключі Companion (Android і iOS одночасно)."""
    std = m.std
    out: dict[str, Any] = {"message": m.body}
    if m.title:
        out["title"] = m.title

    data: dict[str, Any] = {}
    prio = m.priority
    data.update(PRIORITY_ANDROID.get(prio, {}))
    push: dict[str, Any] = {"interruption-level": PRIORITY_IOS.get(prio, "active")}
    if prio == "critical":
        push["sound"] = {"name": "default", "critical": 1, "volume": 1.0}
    if m.silent:
        data["importance"] = "low"
        data.pop("priority", None)
        push = {"interruption-level": "passive", "sound": "none"}
    data["push"] = push

    if m.cat_name:
        data["channel"] = f"Тихо: {m.cat_name}" if m.silent else m.cat_name
        data["group"] = m.category
    if m.icon:
        data["notification_icon"] = m.icon

    tag = m.tag
    progress = std.get("progress")
    if progress and not tag:
        tag = f"progress_{m.category}"
    if tag:
        data["tag"] = tag
    if std.get("sticky"):
        data["sticky"] = "true"
    if progress and progress["percent"] < 100:
        data["progress"] = int(progress["value"])
        data["progress_max"] = int(progress["max"])
        data["alert_once"] = True
        data["sticky"] = "true"

    image = std.get("image")
    if image:
        info = resolve_image(hass, image)
        if info["url"]:
            data["image"] = info["url"]
        else:
            _LOGGER.warning("Картинку %s не можна показати на телефоні", image)

    if std.get("url"):
        data["clickAction"] = std["url"]
        data["url"] = std["url"]
    actions = []
    for a in std.get("actions", []):
        if a.get("url"):
            actions.append({"action": "URI", "title": a["title"], "uri": a["url"]})
        else:
            actions.append({"action": a["id"], "title": a["title"]})
    if actions:
        data["actions"] = actions

    # власні ключі користувача мають пріоритет
    data.update(m.extra)
    out["data"] = data
    return out


async def deliver_phone(hass: HomeAssistant, service: str, m: Msg) -> list[dict]:
    if m.std.get("clear"):
        if not m.tag:
            raise HomeAssistantError("clear потребує tag")
        await hass.services.async_call(
            "notify",
            service,
            {"message": "clear_notification", "data": {"tag": m.tag}},
            blocking=True,
        )
        return []
    payload = phone_payload(hass, m)
    await hass.services.async_call("notify", service, payload, blocking=True)
    return [{"kind": "phone", "service": service, "tag": payload["data"].get("tag")}]


async def phone_clear(hass: HomeAssistant, ref: dict) -> None:
    if ref.get("tag") and hass.services.has_service("notify", ref["service"]):
        await hass.services.async_call(
            "notify",
            ref["service"],
            {"message": "clear_notification", "data": {"tag": ref["tag"]}},
            blocking=True,
        )


# ----------------------------------------------------------------- Telegram


def _tg_target(hass: HomeAssistant, entity_id: str) -> tuple[str | None, int | None]:
    """(config_entry_id бота, chat_id) для сутності notify Telegram."""
    ent = er.async_get(hass).async_get(entity_id)
    if ent is None or not ent.config_entry_id:
        return None, None
    ce = hass.config_entries.async_get_entry(ent.config_entry_id)
    chat = None
    sub_id = getattr(ent, "config_subentry_id", None)
    if ce and sub_id and sub_id in ce.subentries:
        chat = ce.subentries[sub_id].data.get("chat_id")
    try:
        chat = int(chat) if chat is not None else None
    except (TypeError, ValueError):
        chat = None
    return ent.config_entry_id, chat


def _tg_text(m: Msg, fmt: str, bar: str | None = None) -> str:
    """Повний текст з форматованим заголовком, приміткою часу й смугою прогресу."""
    title = m.title or ""
    msg = m.body
    if bar:
        msg = f"{msg}\n{bar}" if msg else bar
    if not title:
        return msg
    if fmt == "html":
        head = f"<b>{content.escape_html(title)}</b>"
    elif fmt == "markdown":
        head = f"*{title}*"
    else:
        head = title
    return f"{head}\n{msg}" if msg else head


def _tg_keyboard(m: Msg) -> list | None:
    if "inline_keyboard" in m.extra:
        kb = m.extra["inline_keyboard"]
        return kb if isinstance(kb, list) else [kb]
    rows = []

    def clean(t: str) -> str:
        return t.replace(":", "∶").replace(",", "‚")

    if m.std.get("url"):
        rows.append(f"Відкрити:{m.std['url']}")
    for a in m.std.get("actions", []):
        if a.get("url"):
            rows.append(f"{clean(a['title'])}:{a['url']}")
        else:
            rows.append(f"{clean(a['title'])}:/{a['id']}")
    return rows or None


async def _tg_send(
    hass: HomeAssistant, entity_id: str, service: str, data: dict
) -> dict | None:
    """Надсилає й повертає {"chat_id", "message_id"} першого чату (або None).

    Спершу «config_entry_id + target» (нова схема), а якщо схема її не приймає,
    «entity_id» (старий спосіб).
    """
    entry_id, chat = _tg_target(hass, entity_id)
    resp = None
    done = False
    if entry_id and chat is not None:
        try:
            resp = await _call(
                hass,
                "telegram_bot",
                service,
                {**data, "config_entry_id": entry_id, "target": [chat]},
                response=True,
            )
            done = True
        except vol.Invalid:
            _LOGGER.debug("telegram_bot.%s: нова схема не підійшла, пробую entity_id", service)
    if not done:
        resp = await _call(
            hass, "telegram_bot", service, {**data, "entity_id": [entity_id]}, response=True
        )
    chats = resp.get("chats") if isinstance(resp, dict) else None
    if chats:
        return {"chat_id": chats[0].get("chat_id"), "message_id": chats[0].get("message_id")}
    return None


async def tg_edit(hass: HomeAssistant, ref: dict, text: str, parse_mode: str) -> None:
    """Редагує текст (або підпис, якщо це фото) уже надісланого повідомлення."""
    base = {
        "config_entry_id": ref["config_entry_id"],
        "chat_id": ref["chat_id"],
        "message_id": ref["message_id"],
        "parse_mode": parse_mode,
    }
    if ref.get("photo"):
        await _call(hass, "telegram_bot", "edit_caption", {**base, "caption": text})
    else:
        await _call(hass, "telegram_bot", "edit_message", {**base, "message": text})


async def deliver_telegram(
    hass: HomeAssistant, state: Any, entity_id: str, m: Msg
) -> list[dict]:
    """Telegram: текст (поділ >4096), фото (підпис <=1024), прогрес із редагуванням."""
    if m.std.get("clear"):
        return []  # у Telegram нема що очищати: раніше надіслане не видаляємо

    std = m.std
    parse_mode = m.extra.get("parse_mode") or PARSE_MODES[std["format"]]
    fmt = "html" if parse_mode == "html" else ("markdown" if "markdown" in parse_mode else "plain")
    entry_id, _chat = _tg_target(hass, entity_id)

    common = {k: m.extra[k] for k in TELEGRAM_PASSTHROUGH if k in m.extra}
    if m.silent:
        common["disable_notification"] = True
    keyboard = _tg_keyboard(m)
    if keyboard:
        common["inline_keyboard"] = keyboard

    prog = std.get("progress")
    pkey = f"{entity_id}|{std.get('tag') or m.category}" if prog else None
    ref = state.progress.get(pkey) if pkey else None

    # --- редагування наявного повідомлення прогресу
    if prog and ref:
        bar = content.progress_bar(prog["percent"])
        try:
            await tg_edit(hass, ref, _tg_text(m, fmt, bar), parse_mode)
            if prog["percent"] < 100:
                return [dict(ref)]
            state.progress.pop(pkey, None)  # завершено: смуга лишається на 100%
            state.save()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Не вдалося відредагувати прогрес у Telegram, надсилаю нове")
            state.progress.pop(pkey, None)

    # смуга лише для проміжного кроку; завершальне повідомлення надсилається без неї
    bar = content.progress_bar(prog["percent"]) if prog and prog["percent"] < 100 else None
    full = _tg_text(m, fmt, bar)

    # --- картинка
    photo_arg: dict | None = None
    tmp_file: str | None = None
    image = std.get("image") or m.extra.get("photo")
    if image:
        info = resolve_image(hass, image)
        if info["camera"]:
            tmp_file = await _camera_to_file(hass, info["camera"])
            photo_arg = {"file": tmp_file}
        elif info["path"]:
            photo_arg = {"file": info["path"]}
        elif info["url"]:
            photo_arg = {"url": info["url"]}
        else:
            raise HomeAssistantError(f"Невідома картинка: {image}")

    try:
        if photo_arg:
            caption_src = m.extra.get("caption")
            first, rest = content.telegram_texts("", caption_src or full, True)
            data = {**photo_arg, "caption": first, "parse_mode": parse_mode, **common}
            data.pop("disable_web_page_preview", None)
            sent = await _tg_send(hass, entity_id, "send_photo", data)
            is_photo = True
        else:
            first, rest = content.telegram_texts("", full, False)
            data = {"message": first, "parse_mode": parse_mode, **common}
            sent = await _tg_send(hass, entity_id, "send_message", data)
            is_photo = False
    finally:
        if tmp_file:
            await _remove_file(hass, tmp_file)

    refs: list[dict] = []
    if sent and entry_id:
        main_ref = {
            "kind": "telegram",
            "entity_id": entity_id,
            "config_entry_id": entry_id,
            "chat_id": sent["chat_id"],
            "message_id": sent["message_id"],
            "photo": is_photo,
            "text": first,
            "parse_mode": parse_mode,
        }
        refs.append(main_ref)
        if pkey and bar:
            state.progress[pkey] = dict(main_ref)
            state.save()

    # --- решта довгого тексту окремими повідомленнями
    tail_common = {k: v for k, v in common.items() if k != "inline_keyboard"}
    for part in rest:
        await _tg_send(
            hass,
            entity_id,
            "send_message",
            {"message": part, "parse_mode": parse_mode, **tail_common},
        )
    return refs


async def tg_mark_expired(hass: HomeAssistant, ref: dict) -> None:
    """Позначає надіслане повідомлення як неактуальне (не видаляє)."""
    fmt = "html" if ref.get("parse_mode") == "html" else "plain"
    text = content.expired_text(ref.get("text", ""), fmt)
    await tg_edit(hass, ref, text, ref.get("parse_mode") or "plain_text")


# ------------------------------------------------------------------ браузер


async def deliver_html5(hass: HomeAssistant, entity_id: str, m: Msg) -> list[dict]:
    if m.std.get("clear"):
        return []
    std = m.std
    extra = dict(m.extra)
    if "requireInteraction" in extra and "require_interaction" not in extra:
        extra["require_interaction"] = extra["requireInteraction"]
    data: dict[str, Any] = {}
    prio = m.priority
    if prio in ("high", "critical"):
        data["require_interaction"] = True
        data["urgency"] = "high"
    if prio == "low":
        data["urgency"] = "low"
    if m.silent:
        data["silent"] = True
    if std.get("sticky"):
        data["require_interaction"] = True
    if std.get("expire"):
        data["ttl"] = int(std["expire"])
    tag = m.tag
    prog = std.get("progress")
    if prog and not tag:
        tag = f"progress_{m.category}"
    if tag:
        data["tag"] = tag
    if std.get("image"):
        info = resolve_image(hass, std["image"])
        if info["url"] and not info["camera"]:
            data["image"] = info["url"]
    if std.get("url"):
        data["data"] = {"url": std["url"]}
    if std.get("actions"):
        data["actions"] = [
            {"action": a.get("id") or "open", "title": a["title"]} for a in std["actions"]
        ][:2]
    for k in (*HTML5_PASSTHROUGH, "image", "tag", "actions"):
        if k in extra:
            data[k] = extra[k]

    message = m.body
    if prog and prog["percent"] < 100:
        message = f"{message}\n{content.progress_bar(prog['percent'])}"
    data["title"] = m.title or "Home Assistant"
    data["message"] = message
    await hass.services.async_call(
        "html5", "send_message", data, target={"entity_id": entity_id}, blocking=True
    )
    return []


# ------------------------------------------------------- інші сервіси notify


async def deliver_generic_service(hass: HomeAssistant, service: str, m: Msg) -> list[dict]:
    if m.std.get("clear"):
        return []
    out: dict[str, Any] = {"message": m.body}
    if m.title:
        out["title"] = m.title
    if service == "persistent_notification":
        nid = f"smart_notify_{m.tag}" if m.tag else f"smart_notify_{m.id}"
        prog = m.std.get("progress")
        if prog and prog["percent"] < 100:
            out["message"] = f"{m.body}\n{content.progress_bar(prog['percent'])}"
        out["data"] = {"notification_id": nid}
        await hass.services.async_call("notify", service, out, blocking=True)
        return [{"kind": "persistent", "nid": nid}]
    if m.raw:
        out["data"] = dict(m.raw)
    await hass.services.async_call("notify", service, out, blocking=True)
    return []


async def persistent_dismiss(hass: HomeAssistant, ref: dict) -> None:
    if hass.services.has_service("persistent_notification", "dismiss"):
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": ref["nid"]},
            blocking=True,
        )


async def deliver_generic_entity(hass: HomeAssistant, entity_id: str, m: Msg) -> list[dict]:
    if m.std.get("clear"):
        return []
    data = {"entity_id": entity_id, "message": m.body}
    if m.title:
        data["title"] = m.title
    await hass.services.async_call("notify", "send_message", data, blocking=True)
    return []


# --------------------------------------------------------------- точка входу


async def deliver(hass: HomeAssistant, state: Any, source: str, m: Msg) -> list[dict]:
    """Надсилає повідомлення через джерело `service:<ім'я>[@пристрій]` або `entity:<id>`."""
    kind, _, target = source.partition(":")
    if kind == "entity":
        platform = _entity_platform(hass, target)
        if platform == "telegram_bot" and hass.services.has_service(
            "telegram_bot", "send_message"
        ):
            return await deliver_telegram(hass, state, target, m)
        if platform == "html5" and hass.services.has_service("html5", "send_message"):
            return await deliver_html5(hass, target, m)
        return await deliver_generic_entity(hass, target, m)

    service, _, device = target.partition("@")
    if service.startswith("mobile_app_") and not device:
        return await deliver_phone(hass, service, m)
    return await deliver_generic_service(hass, service, m)


async def clear_refs(hass: HomeAssistant, refs: list[dict]) -> None:
    """Знімає надіслане: телефон очищається, Telegram позначається, панель закривається."""
    for ref in refs:
        try:
            if ref["kind"] == "phone":
                await phone_clear(hass, ref)
            elif ref["kind"] == "telegram":
                await tg_mark_expired(hass, ref)
            elif ref["kind"] == "persistent":
                await persistent_dismiss(hass, ref)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Не вдалося зняти сповіщення %s", ref.get("kind"), exc_info=True)


__all__ = ["Msg", "deliver", "clear_refs", "FALLBACK_SOURCE"]
