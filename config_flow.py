"""Config flow: два записи (канали, категорії), підзаписи, маршрути (options)."""
from __future__ import annotations

import re
from copy import deepcopy

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    IconSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    COMMON,
    DOMAIN,
    ENTRY_TITLES,
    KIND_CATEGORIES,
    KIND_CHANNELS,
    KINDS,
)
from .helpers import (
    PERSISTENT,
    PERSISTENT_LABEL,
    available_sources,
    channels_of,
    entry_of,
    norm_setting,
    persons,
)

ID_RE = re.compile(r"^[a-z0-9_]+$")
GENERAL_KEY = "__general"
DONE_KEY = "__done"

MODE_LABELS = {
    "inherit": "Як у загальному маршруті",
    "off": "Вимкнено",
    "send": "Надсилати",
}
MODE_LABELS_GENERAL = {
    "inherit": "Не налаштовано (лише панель HA)",
    "off": "Вимкнено",
    "send": "Надсилати",
}


def sub_title(hass: HomeAssistant, subentry_type: str, data: dict) -> str:
    """Канал: «Власник · Назва», категорія: «Назва»."""
    if subentry_type == "channel":
        owner = data["owner"]
        label = "Загальний" if owner == COMMON else persons(hass).get(owner, owner)
        return f"{label} · {data['name']}"
    return data["name"]


def _select(options: dict[str, str], multiple: bool = False) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=k, label=v) for k, v in options.items()],
            mode=SelectSelectorMode.DROPDOWN,
            multiple=multiple,
        )
    )


def _owners(hass) -> dict[str, str]:
    return {COMMON: "Загальний (для всіх)", **persons(hass)}


class SmartNotifyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        missing = [k for k in KINDS if entry_of(self.hass, k) is None]
        if not missing:
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            return await self._create(missing[0])
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_import(self, import_data):
        return await self._create(import_data["kind"], import_data)

    async def _create(self, kind: str, extra: dict | None = None):
        await self.async_set_unique_id(kind)
        self._abort_if_unique_id_configured()
        extra = extra or {}
        subentries = [
            {
                "data": dict(c),
                "subentry_type": "category",
                "title": sub_title(self.hass, "category", c),
                "unique_id": c["id"],
            }
            for c in extra.get("categories", [])
        ]
        options = {"routes": extra["routes"]} if extra.get("routes") else {}
        return self.async_create_entry(
            title=ENTRY_TITLES[kind],
            data={"kind": kind},
            options=options,
            subentries=subentries,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry):
        kind = config_entry.data.get("kind")
        if kind == KIND_CHANNELS:
            return {"channel": ChannelFlow}
        if kind == KIND_CATEGORIES:
            return {"category": CategoryFlow}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoutesFlow()


class ChannelFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        entry = self._get_entry()
        used = {
            s.data["source"]
            for s in entry.subentries.values()
            if s.subentry_type == "channel"
        }
        free = {k: v for k, v in available_sources(self.hass).items() if k not in used}
        if not free:
            return self.async_abort(reason="no_sources")
        if user_input is not None:
            return self.async_create_entry(
                title=sub_title(self.hass, "channel", user_input),
                data=user_input,
                unique_id=user_input["source"],
            )
        schema = vol.Schema(
            {
                vol.Required("source"): _select(free),
                vol.Required("name"): TextSelector(),
                vol.Required("owner"): _select(_owners(self.hass)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(self, user_input=None):
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            new = {**sub.data, **user_input}
            return self.async_update_and_abort(
                self._get_entry(),
                sub,
                title=sub_title(self.hass, "channel", new),
                data=new,
            )
        schema = vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Required("owner"): _select(_owners(self.hass)),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, sub.data),
        )


class CategoryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            cid = user_input["id"].strip()
            existing = {
                s.data["id"]
                for s in self._get_entry().subentries.values()
                if s.subentry_type == "category"
            }
            if not ID_RE.match(cid):
                errors["id"] = "bad_id"
            elif cid in existing:
                errors["id"] = "id_exists"
            else:
                data = {**user_input, "id": cid}
                return self.async_create_entry(
                    title=sub_title(self.hass, "category", data),
                    data=data,
                    unique_id=cid,
                )
        schema = vol.Schema(
            {
                vol.Required("id"): TextSelector(),
                vol.Required("name"): TextSelector(),
                vol.Optional("icon"): IconSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            new = {**sub.data, **user_input}
            return self.async_update_and_abort(
                self._get_entry(),
                sub,
                title=sub_title(self.hass, "category", new),
                data=new,
            )
        schema = vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Optional("icon"): IconSelector(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, sub.data),
        )


class RoutesFlow(OptionsFlow):
    """Маршрути: користувач → (загальний або категорія) → режим/канали/резерв."""

    def __init__(self) -> None:
        self._user: str | None = None
        self._target: str | None = None
        self._draft: dict = {}
        self._channels: dict[str, str] = {}  # id -> назва (власні + «Панель HA»)

    def _cats(self) -> dict[str, str]:
        return {
            s.data["id"]: s.data["name"]
            for s in self.config_entry.subentries.values()
            if s.subentry_type == "category"
        }

    def _summary(self, setting: dict, general: bool) -> str:
        if setting["mode"] == "off":
            return "вимкнено"
        if setting["mode"] == "inherit":
            return "лише панель HA" if general else "як у загальному"
        names = self._channels
        txt = ", ".join(names.get(c, c) for c in setting["channels"]) or "—"
        if setting["fallback"]:
            txt += " → резерв: " + ", ".join(
                names.get(c, c) for c in setting["fallback"]
            )
        return txt

    async def async_step_init(self, user_input=None):
        if self.config_entry.data.get("kind") != KIND_CATEGORIES:
            return self.async_abort(reason="routes_on_categories")
        people = persons(self.hass)
        if not people:
            return self.async_abort(reason="no_users")
        if user_input is not None:
            self._user = user_input["user"]
            self._channels = {
                sid: d["name"]
                for sid, d in channels_of(self.hass).items()
                if d["owner"] == self._user
            }
            self._channels[PERSISTENT] = PERSISTENT_LABEL
            allowed = set(self._channels)

            cur = self.config_entry.options.get("routes", {}).get(self._user, {})
            self._draft = {
                "default": norm_setting(cur.get("default"), allowed),
                "cats": {
                    cid: norm_setting(cur.get("cats", {}).get(cid), allowed)
                    for cid in self._cats()
                },
            }
            return await self.async_step_pick()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("user"): _select(people)}),
        )

    async def async_step_pick(self, user_input=None):
        if user_input is not None:
            target = user_input["target"]
            if target == DONE_KEY:
                new = deepcopy(dict(self.config_entry.options))
                new.setdefault("routes", {})[self._user] = self._draft
                return self.async_create_entry(data=new)
            self._target = target
            return await self.async_step_edit()

        options = {
            GENERAL_KEY: "Загальний маршрут: "
            + self._summary(self._draft["default"], True)
        }
        for cid, name in self._cats().items():
            options[cid] = f"{name}: " + self._summary(self._draft["cats"][cid], False)
        options[DONE_KEY] = "✓ Зберегти й вийти"

        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {vol.Required("target", default=DONE_KEY): _select(options)}
            ),
            description_placeholders={"user": persons(self.hass)[self._user]},
        )

    async def async_step_edit(self, user_input=None):
        general = self._target == GENERAL_KEY
        cur = (
            self._draft["default"]
            if general
            else self._draft["cats"][self._target]
        )

        if user_input is not None:
            new = norm_setting(
                {
                    "mode": user_input.get("mode", "inherit"),
                    "channels": user_input.get("channels", []),
                    "fallback": user_input.get("fallback", []),
                },
                set(self._channels),
            )
            if general:
                self._draft["default"] = new
            else:
                self._draft["cats"][self._target] = new
            return await self.async_step_pick()

        modes = MODE_LABELS_GENERAL if general else MODE_LABELS
        schema = vol.Schema(
            {
                vol.Required("mode"): _select(modes),
                vol.Optional("channels"): _select(self._channels, multiple=True),
                vol.Optional("fallback"): _select(self._channels, multiple=True),
            }
        )
        title = "Загальний маршрут" if general else self._cats()[self._target]
        return self.async_show_form(
            step_id="edit",
            data_schema=self.add_suggested_values_to_schema(schema, cur),
            description_placeholders={
                "user": persons(self.hass)[self._user],
                "target": title,
            },
        )