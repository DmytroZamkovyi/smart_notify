"""Допоміжні функції: дані записів, користувачі, доступні способи, доставка."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import DOMAIN, EXCLUDED_SERVICES, KIND_CATEGORIES, KIND_CHANNELS
from .routing import MODES, category_map, norm_setting  # noqa: F401  (реекспорт)

_LOGGER = logging.getLogger(__name__)

# Вбудований «канал»: персистентне сповіщення в панелі HA (доступний усім)
PERSISTENT = "persistent"
PERSISTENT_LABEL = "Панель HA (персистентне)"

PLATFORM_NAMES = {
    "telegram_bot": "Telegram",
    "telegram": "Telegram",
    "html5": "Браузер",
}

# Ключі data, які передаються в дії telegram_bot як є
TELEGRAM_PASSTHROUGH = (
    "parse_mode",
    "disable_notification",
    "disable_web_page_preview",
    "keyboard",
    "inline_keyboard",
    "message_tag",
    "reply_to_message_id",
    "message_thread_id",
)

# Ключі data, які передаються в html5.send_message як є
HTML5_PASSTHROUGH = (
    "icon",
    "badge",
    "image",
    "tag",
    "actions",
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


def entry_of(hass: HomeAssistant, kind: str) -> ConfigEntry | None:
    """Запис інтеграції певного виду (канали або категорії)."""
    for e in hass.config_entries.async_entries(DOMAIN):
        if e.data.get("kind") == kind:
            return e
    return None


def channels_of(hass: HomeAssistant) -> dict[str, dict]:
    """id підзапису каналу -> дані каналу."""
    e = entry_of(hass, KIND_CHANNELS)
    if e is None:
        return {}
    return {
        sid: dict(s.data)
        for sid, s in e.subentries.items()
        if s.subentry_type == "channel"
    }


def categories_of(hass: HomeAssistant) -> list[dict]:
    """Сирі дані категорій і підкатегорій (як збережено)."""
    e = entry_of(hass, KIND_CATEGORIES)
    if e is None:
        return []
    return [dict(s.data) for s in e.subentries.values() if s.subentry_type == "category"]


def category_tree(hass: HomeAssistant) -> dict[str, dict]:
    """id -> нормалізована категорія (з parent, prefix, default, service)."""
    return category_map(categories_of(hass))


def routes_of(hass: HomeAssistant) -> dict:
    e = entry_of(hass, KIND_CATEGORIES)
    return dict(e.options.get("routes", {})) if e else {}


def persons(hass: HomeAssistant) -> dict[str, str]:
    """object_id person -> ім'я."""
    return {s.object_id: s.name for s in hass.states.async_all("person")}


def _entity_platform(hass: HomeAssistant, entity_id: str) -> str | None:
    ent = er.async_get(hass).async_get(entity_id)
    return ent.platform if ent else None


def available_sources(hass: HomeAssistant) -> dict[str, str]:
    """Усе, чим HA вміє сповіщати: id джерела -> людська назва."""
    out: dict[str, str] = {}

    notify_states = sorted(
        hass.states.async_all("notify"), key=lambda s: s.entity_id
    )
    # якщо є сутності HTML5 - старі сервіси notify.html5* є лише дублікатами
    has_html5_entities = any(
        _entity_platform(hass, s.entity_id) == "html5" for s in notify_states
    )

    phones: dict[str, str] = {}
    for e in hass.config_entries.async_entries("mobile_app"):
        name = e.data.get("device_name")
        if name:
            phones[f"mobile_app_{slugify(name)}"] = name

    for svc in sorted(hass.services.async_services().get("notify", {})):
        if svc in EXCLUDED_SERVICES:
            continue
        if has_html5_entities and (svc == "html5" or svc.startswith("html5_")):
            continue
        if svc in phones:
            out[f"service:{svc}"] = f"Телефон: {phones[svc]}"
        else:
            out[f"service:{svc}"] = f"Сервіс notify.{svc}"

    registry = er.async_get(hass)
    for st in notify_states:
        ent = registry.async_get(st.entity_id)
        platform = ent.platform if ent else "?"
        if platform == "mobile_app":  # телефон уже є як сервіс
            continue
        title = ""
        if ent and ent.config_entry_id:
            ce = hass.config_entries.async_get_entry(ent.config_entry_id)
            if ce:
                title = ce.title
        kind = PLATFORM_NAMES.get(platform, platform)
        owner = f" «{title}»" if title else ""
        out[f"entity:{st.entity_id}"] = f"{kind}{owner}: {st.name} ({st.entity_id})"

    return out


async def _deliver_telegram(
    hass: HomeAssistant, entity_id: str, payload: dict
) -> None:
    """Telegram-сутність: data перетворюється на дії telegram_bot.*.

    Підтримується: photo (або image) - URL чи локальний шлях, caption,
    parse_mode, keyboard, inline_keyboard, disable_notification,
    disable_web_page_preview, message_tag, reply_to_message_id, message_thread_id.
    """
    extra = payload.get("data") or {}
    message = payload["message"]
    title = payload.get("title")

    common = {k: extra[k] for k in TELEGRAM_PASSTHROUGH if k in extra}
    photo = extra.get("photo") or extra.get("image")

    if photo:
        caption = extra.get("caption")
        if caption is None:
            caption = f"{title}\n{message}" if title else message
        data = {"entity_id": [entity_id], "caption": caption, **common}
        data.pop("disable_web_page_preview", None)  # для фото не діє
        if str(photo).startswith(("http://", "https://")):
            data["url"] = photo
        else:
            data["file"] = photo
        await hass.services.async_call(
            "telegram_bot", "send_photo", data, blocking=True
        )
        return

    data = {"entity_id": [entity_id], "message": message, **common}
    if title:
        data["title"] = title
    await hass.services.async_call(
        "telegram_bot", "send_message", data, blocking=True
    )


async def _deliver_html5(hass: HomeAssistant, entity_id: str, payload: dict) -> None:
    """Браузер (сутність HTML5): data -> html5.send_message.

    Підтримується: icon, badge, image, tag, actions, dir, renotify, silent,
    require_interaction (або requireInteraction), vibrate, lang, timestamp,
    ttl, urgency, data.
    """
    extra = dict(payload.get("data") or {})
    if "requireInteraction" in extra and "require_interaction" not in extra:
        extra["require_interaction"] = extra["requireInteraction"]

    data = {k: extra[k] for k in HTML5_PASSTHROUGH if k in extra}
    data["title"] = payload.get("title") or "Home Assistant"  # title обов'язковий
    data["message"] = payload["message"]
    await hass.services.async_call(
        "html5",
        "send_message",
        data,
        target={"entity_id": entity_id},
        blocking=True,
    )


async def deliver(hass: HomeAssistant, source: str, payload: dict) -> None:
    """Надсилає через сервіс notify.* або notify-сутність.

    Джерело service:html5@Ім'я (застарілий варіант) теж розуміється.
    """
    kind, _, target = source.partition(":")
    if kind == "entity":
        platform = _entity_platform(hass, target)
        if platform == "telegram_bot" and hass.services.has_service(
            "telegram_bot", "send_message"
        ):
            await _deliver_telegram(hass, target, payload)
            return
        if platform == "html5" and hass.services.has_service("html5", "send_message"):
            await _deliver_html5(hass, target, payload)
            return

        data = {"entity_id": target, "message": payload["message"]}
        if "title" in payload:
            data["title"] = payload["title"]
        if "data" in payload:
            _LOGGER.debug("data відкинуто для сутності %s", target)
        await hass.services.async_call("notify", "send_message", data, blocking=True)
        return

    service, _, device = target.partition("@")
    out = dict(payload)
    if device:
        out["target"] = [device]
    await hass.services.async_call("notify", service, out, blocking=True)