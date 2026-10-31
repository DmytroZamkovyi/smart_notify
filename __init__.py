"""Smart Notify: єдина точка сповіщень з маршрутизацією по користувачах і категоріях."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components import (
    frontend,
    panel_custom,
    persistent_notification,
    websocket_api,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.start import async_at_started
from homeassistant.util import slugify

from .config_flow import sub_title
from .const import (
    DOMAIN,
    ENTRY_TITLES,
    KIND_CATEGORIES,
    KIND_CHANNELS,
    KINDS,
)
from .dispatch import Notifier, notifier_of
from .helpers import (
    PERSISTENT,
    category_tree,
    channels_of,
    entry_of,
    persons,
    routes_of,
    settings_of,
)
from . import quiet as quiet_mod
from .routing import (
    admin_off,
    dedup_minutes,
    group_ids,
    icon_of,
    label,
    norm_setting,
    ordered_ids,
)

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "smart-notify"
STATIC_URL = "/smart_notify_static/panel.js"

SEND_SCHEMA = vol.Schema(
    {
        vol.Optional("user"): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("category"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("data"): dict,
        vol.Optional("presence", default="any"): vol.In(["any", "home", "away"]),
        vol.Optional("exclude"): vol.All(cv.ensure_list, [cv.string]),
    }
)


def _service_schema(hass: HomeAssistant) -> dict:
    """Опис сервісу для форми в UI: користувачі й категорії зі списку.

    У списку лише категорії, на які можна надсилати: підкатегорії (під батьком)
    і звичайні категорії без підкатегорій. Категорії-групи не показуються.
    """
    cats = category_tree(hass)
    groups = group_ids(cats)
    options = [
        {"value": i, "label": label(cats, i)}
        for i in ordered_ids(cats)
        if i not in groups
    ]
    return {
        "name": "Надіслати сповіщення (за маршрутами)",
        "description": "Надсилає сповіщення за маршрутами користувачів.",
        "fields": {
            "user": {
                "name": "Користувач",
                "description": "Порожньо: загальні канали, а якщо їх немає, усім.",
                "required": False,
                "selector": {"entity": {"domain": "person", "multiple": True}},
            },
            "category": {
                "name": "Категорія",
                "description": "Категорія без підкатегорій або підкатегорія.",
                "required": True,
                "selector": {
                    "select": {
                        "options": options,
                        "custom_value": True,
                        "mode": "dropdown",
                    }
                },
            },
            "message": {
                "name": "Повідомлення",
                "description": "Текст сповіщення.",
                "required": True,
                "selector": {"text": {"multiline": True}},
            },
            "title": {
                "name": "Заголовок",
                "description": "Необов'язково.",
                "required": False,
                "selector": {"text": {}},
            },
            "presence": {
                "name": "Присутність",
                "description": "Лише тим, хто вдома або не вдома (за person).",
                "required": False,
                "default": "any",
                "selector": {
                    "select": {
                        "options": [
                            {"value": "any", "label": "Усім"},
                            {"value": "home", "label": "Лише вдома"},
                            {"value": "away", "label": "Лише не вдома"},
                        ],
                        "mode": "dropdown",
                    }
                },
            },
            "exclude": {
                "name": "Виключити",
                "description": "Кому не надсилати.",
                "required": False,
                "selector": {"entity": {"domain": "person", "multiple": True}},
            },
            "data": {
                "name": "Дані",
                "description": (
                    "image, url, actions, tag, priority, format, sticky, clear, "
                    "expire, progress (+ ключі каналів)."
                ),
                "required": False,
                "selector": {"object": {}},
            },
        },
    }


@callback
def _apply_schema(hass: HomeAssistant) -> None:
    async_set_service_schema(hass, "notify", "smart", _service_schema(hass))


def _person_of(hass: HomeAssistant, ha_user_id: str) -> str | None:
    """object_id person, прив'язаної до облікового запису HA."""
    for s in hass.states.async_all("person"):
        if s.attributes.get("user_id") == ha_user_id:
            return s.object_id
    return None


def _editable(hass: HomeAssistant, connection) -> tuple[dict[str, str], str | None]:
    """Кого може редагувати цей користувач, і хто він сам."""
    people = persons(hass)
    me = _person_of(hass, connection.user.id)
    if connection.user.is_admin:
        return people, me
    if me and me in people:
        return {me: people[me]}, me
    return {}, me


def _allowed_for(hass: HomeAssistant, user: str) -> set[str]:
    """Id каналів, які може вибрати користувач: власні + «Панель HA»."""
    own = {sid for sid, d in channels_of(hass).items() if d["owner"] == user}
    return own | {PERSISTENT}


@callback
def _ws_get(hass: HomeAssistant, connection, msg: dict) -> None:
    if entry_of(hass, KIND_CATEGORIES) is None:
        connection.send_error(msg["id"], "not_loaded", "Інтеграція не налаштована")
        return
    users, me = _editable(hass, connection)

    tree = category_tree(hass)
    groups = group_ids(tree)
    is_admin = connection.user.is_admin
    # дерево: категорія, під нею її підкатегорії; службові бачить лише адмін
    cats = [
        {
            "id": i,
            "name": tree[i]["name"],
            "parent": tree[i]["parent"],
            "parent_name": tree[tree[i]["parent"]]["name"] if tree[i]["parent"] else None,
            "group": i in groups,
            "default": tree[i]["default"],
            "service": tree[i]["service"],
            "admin_off": admin_off(tree, i),
            "icon": icon_of(tree, i),
            "dedup": dedup_minutes(tree, i),
        }
        for i in ordered_ids(tree)
        if is_admin or not tree[i]["service"]
    ]

    channels: dict[str, list[dict]] = {u: [] for u in users}
    for sid, data in channels_of(hass).items():
        if data["owner"] in channels:
            channels[data["owner"]].append({"id": sid, "name": data["name"]})

    stored = routes_of(hass)
    routes: dict[str, dict] = {}
    for u in users:
        allowed = _allowed_for(hass, u)
        r = stored.get(u, {})
        routes[u] = {
            "default": norm_setting(r.get("default"), allowed),
            "cats": {
                c["id"]: norm_setting(r.get("cats", {}).get(c["id"]), allowed)
                for c in cats
            },
            "quiet": quiet_mod.norm_quiet(r.get("quiet")),
        }

    connection.send_result(
        msg["id"],
        {
            "is_admin": connection.user.is_admin,
            "me": me,
            "users": users,
            "categories": cats,
            "channels": channels,
            "routes": routes,
            "settings": settings_of(hass),
            "queue": {u: notifier_of(hass).queue_count(u) for u in users},
        },
    )


@callback
def _ws_set(hass: HomeAssistant, connection, msg: dict) -> None:
    entry = entry_of(hass, KIND_CATEGORIES)
    users, _ = _editable(hass, connection)
    user = msg["user"]
    if entry is None or user not in users:
        connection.send_error(msg["id"], "unauthorized", "Немає доступу")
        return

    allowed = _allowed_for(hass, user)
    tree = category_tree(hass)
    # службові категорії змінює лише адмін; у решти користувачів вони лишаються як є
    editable = {
        i for i, c in tree.items() if connection.user.is_admin or not c["service"]
    }
    stored_cats = routes_of(hass).get(user, {}).get("cats", {})

    new_cats = {
        i: norm_setting(stored_cats[i], allowed)
        for i in tree
        if i in stored_cats and i not in editable
    }
    for c, v in msg["cats"].items():
        if c in editable:
            new_cats[c] = norm_setting(v, allowed)

    stored_quiet = routes_of(hass).get(user, {}).get("quiet")
    new_route = {
        "default": norm_setting(msg["default"], allowed),
        "cats": new_cats,
        "quiet": quiet_mod.norm_quiet(msg["quiet"] if "quiet" in msg else stored_quiet),
    }
    options = dict(entry.options)
    routes = dict(options.get("routes", {}))
    routes[user] = new_route
    options["routes"] = routes
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], new_route)


@callback
def _ws_journal(hass: HomeAssistant, connection, msg: dict) -> None:
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Лише для адміна")
        return
    connection.send_result(msg["id"], {"items": notifier_of(hass).journal()})


@websocket_api.async_response
async def _ws_test(hass: HomeAssistant, connection, msg: dict) -> None:
    users, _ = _editable(hass, connection)
    user = msg["user"]
    if user not in users:
        connection.send_error(msg["id"], "unauthorized", "Немає доступу")
        return
    cat = msg["category"]
    try:
        result = await notifier_of(hass).send(
            {
                "category": cat,
                "user": [user],
                "title": "Тест Smart Notify",
                "message": "Перевірка доставки за вашим маршрутом.",
                "data": {"priority": msg.get("priority", "normal")},
            },
            test=True,
        )
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


async def _register_common(hass: HomeAssistant) -> None:
    """Сервіс, websocket і сторінка: спільні для обох записів, реєструються раз.

    Обидва записи стартують одночасно, тому все під блокуванням.
    """
    data = hass.data.setdefault(DOMAIN, {})
    lock = data.setdefault("reg_lock", asyncio.Lock())

    async with lock:
        if "notifier" not in data:
            notifier = Notifier(hass)
            await notifier.async_start()
            data["notifier"] = notifier

        if not data.get("svc"):

            async def _send(call: ServiceCall) -> ServiceResponse:
                return await notifier_of(hass).send(dict(call.data))

            hass.services.async_register(
                "notify",
                "smart",
                _send,
                schema=SEND_SCHEMA,
                supports_response=SupportsResponse.OPTIONAL,
            )
            data["svc"] = True

        if not data.get("static"):
            js = Path(__file__).parent / "panel.js"
            await hass.http.async_register_static_paths(
                [StaticPathConfig(STATIC_URL, str(js), False)]
            )
            data["static"] = True
            data["version"] = int(
                (await hass.async_add_executor_job(js.stat)).st_mtime
            )

        if not data.get("ws"):
            websocket_api.async_register_command(
                hass,
                "smart_notify/get",
                _ws_get,
                websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                    {vol.Required("type"): "smart_notify/get"}
                ),
            )
            websocket_api.async_register_command(
                hass,
                "smart_notify/set",
                _ws_set,
                websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                    {
                        vol.Required("type"): "smart_notify/set",
                        vol.Required("user"): str,
                        vol.Required("default"): dict,
                        vol.Required("cats"): {str: dict},
                        vol.Optional("quiet"): dict,
                    }
                ),
            )
            websocket_api.async_register_command(
                hass,
                "smart_notify/journal",
                _ws_journal,
                websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                    {vol.Required("type"): "smart_notify/journal"}
                ),
            )
            websocket_api.async_register_command(
                hass,
                "smart_notify/test",
                _ws_test,
                websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                    {
                        vol.Required("type"): "smart_notify/test",
                        vol.Required("user"): str,
                        vol.Required("category"): str,
                        vol.Optional("priority", default="normal"): vol.In(
                            ["low", "normal", "high", "critical"]
                        ),
                    }
                ),
            )
            data["ws"] = True

        if not data.get("panel"):
            await panel_custom.async_register_panel(
                hass,
                webcomponent_name="smart-notify-panel",
                frontend_url_path=PANEL_URL,
                module_url=f"{STATIC_URL}?v={data['version']}",
                sidebar_title=None,
                sidebar_icon=None,
                require_admin=False,
            )
            data["panel"] = True


async def _refresh_titles(hass: HomeAssistant) -> None:
    """Оновлює заголовки в списках: канали «Власник · Назва», підкатегорії «Батько › Назва»."""
    for kind, sub_type in ((KIND_CHANNELS, "channel"), (KIND_CATEGORIES, "category")):
        entry = entry_of(hass, kind)
        if entry is None:
            continue
        for sub in list(entry.subentries.values()):
            if sub.subentry_type != sub_type:
                continue
            title = sub_title(hass, sub_type, dict(sub.data))
            if title != sub.title:
                hass.config_entries.async_update_subentry(entry, sub, title=title)


async def _heal_categories(hass: HomeAssistant) -> None:
    """Підкатегорії, чия батьківська категорія зникла, стають звичайними категоріями.

    Ядро HA не дає заборонити стандартне видалення підзапису, тому наслідки
    прибираємо після видалення й повідомляємо про це в панелі HA.
    """
    entry = entry_of(hass, KIND_CATEGORIES)
    if entry is None:
        return
    subs = [s for s in entry.subentries.values() if s.subentry_type == "category"]
    ids = {s.data.get("id") for s in subs}
    healed: list[str] = []
    for s in subs:
        parent = s.data.get("parent")
        if parent and (parent not in ids or parent == s.data.get("id")):
            data = {k: v for k, v in s.data.items() if k != "parent"}
            hass.config_entries.async_update_subentry(
                entry, s, data=data, title=sub_title(hass, "category", data)
            )
            healed.append(f"{data['name']} ({data['id']})")
    if healed:
        persistent_notification.async_create(
            hass,
            "Видалено категорію, у якої були підкатегорії. Вони стали звичайними "
            "категоріями верхнього рівня: " + ", ".join(healed) + ". "
            "Перевір скрипти й налаштування маршрутів.",
            title="Smart Notify",
            notification_id="smart_notify_orphaned_categories",
        )


async def _ensure_kinds(hass: HomeAssistant) -> None:
    """Створює відсутній запис і переносить категорії зі старого єдиного запису."""
    lock = hass.data.setdefault(DOMAIN, {}).setdefault("lock", asyncio.Lock())
    async with lock:
        ch = entry_of(hass, KIND_CHANNELS)
        legacy_cats = (
            [
                dict(s.data)
                for s in ch.subentries.values()
                if s.subentry_type == "category"
            ]
            if ch
            else []
        )
        legacy_routes = dict(ch.options.get("routes", {})) if ch else {}

        for kind in KINDS:
            if entry_of(hass, kind) is not None:
                continue
            data: dict = {"kind": kind}
            if kind == KIND_CATEGORIES:
                data["categories"] = legacy_cats
                data["routes"] = legacy_routes
            res = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=data
            )
            if res["type"] != FlowResultType.CREATE_ENTRY:
                _LOGGER.warning("Не вдалося створити запис %s: %s", kind, res)

        # старі категорії й маршрути прибираємо із записів каналів лише після переносу
        if ch is not None and entry_of(hass, KIND_CATEGORIES) is not None:
            for sid, s in list(ch.subentries.items()):
                if s.subentry_type == "category":
                    hass.config_entries.async_remove_subentry(ch, sid)
            if ch.options.get("routes"):
                hass.config_entries.async_update_entry(ch, options={})

        _apply_schema(hass)


async def _on_entry_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Категорії змінились: зцілюємо підкатегорії, оновлюємо заголовки й форму сервісу."""
    await _heal_categories(hass)
    await _refresh_titles(hass)
    _apply_schema(hass)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if "kind" not in entry.data:  # запис зі старої версії: це тепер запис каналів
        hass.config_entries.async_update_entry(
            entry,
            data={"kind": KIND_CHANNELS},
            title=ENTRY_TITLES[KIND_CHANNELS],
            unique_id=KIND_CHANNELS,
        )

    await _register_common(hass)
    _apply_schema(hass)
    entry.async_on_unload(entry.add_update_listener(_on_entry_update))

    async def _on_started(_hass: HomeAssistant) -> None:
        await _refresh_titles(hass)
        _apply_schema(hass)

    entry.async_on_unload(async_at_started(hass, _on_started))
    hass.async_create_task(_ensure_kinds(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if others:
        return
    hass.services.async_remove("notify", "smart")
    frontend.async_remove_panel(hass, PANEL_URL)
    data = hass.data.get(DOMAIN, {})
    if "notifier" in data:
        data.pop("notifier").async_stop()
    data["svc"] = False
    data["panel"] = False