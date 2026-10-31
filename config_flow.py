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
    BooleanSelector,
    IconSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    persons,
    settings_of,
)
from .routing import (
    DEFAULT_GENERAL,
    DEFAULT_OFF,
    admin_off,
    category_map,
    group_ids,
    label as cat_label,
    norm_setting,
    ordered_ids,
)

ID_RE = re.compile(r"^[a-z0-9_]+$")
GENERAL_KEY = "__general"
DONE_KEY = "__done"

# необов'язкові ключі підзапису категорії: очищене поле у формі означає «видалити»
CATEGORY_OPTIONAL = ("icon", "parent", "prefix")
DEDUP_MAX = 1440

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
    """Канал: «Власник · Назва», підкатегорія: «Батько › Назва», категорія: «Назва»."""
    if subentry_type == "channel":
        owner = data["owner"]
        label = "Загальний" if owner == COMMON else persons(hass).get(owner, owner)
        return f"{label} · {data['name']}"
    parent = data.get("parent")
    if parent:
        entry = entry_of(hass, KIND_CATEGORIES)
        if entry is not None:
            for s in entry.subentries.values():
                if s.subentry_type == "category" and s.data.get("id") == parent:
                    return f"{s.data['name']} › {data['name']}"
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


def _default_select() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[DEFAULT_GENERAL, DEFAULT_OFF],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="category_default",
        )
    )


def _category_data(base: dict, user_input: dict) -> dict:
    """Збирає дані категорії: необов'язкові поля з форми замінюють старі, порожні прибираються."""
    data = {k: v for k, v in base.items() if k not in CATEGORY_OPTIONAL}
    for key in CATEGORY_OPTIONAL:
        value = user_input.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            data[key] = value
    data["name"] = user_input["name"].strip()
    data["default"] = user_input.get("default", DEFAULT_GENERAL)
    data["service"] = bool(user_input.get("service", False))
    dedup = int(user_input.get("dedup") or 0)
    if dedup > 0:
        data["dedup"] = min(dedup, DEDUP_MAX)
    else:
        data.pop("dedup", None)
    return data


class CategoryFlow(ConfigSubentryFlow):
    """Категорія або підкатегорія (з необов'язковим полем «батьківська категорія»)."""

    def _subs(self) -> list[dict]:
        return [
            dict(s.data)
            for s in self._get_entry().subentries.values()
            if s.subentry_type == "category"
        ]

    def _parents(self, exclude: str | None = None) -> dict[str, str]:
        """Категорії верхнього рівня, які можуть бути батьком."""
        return {
            c["id"]: c["name"]
            for c in self._subs()
            if not c.get("parent") and c["id"] != exclude
        }

    def _schema(self, with_id: bool, exclude: str | None = None) -> vol.Schema:
        fields: dict = {}
        if with_id:
            fields[vol.Required("id")] = TextSelector()
        fields[vol.Required("name")] = TextSelector()
        fields[vol.Optional("icon")] = IconSelector()
        parents = self._parents(exclude)
        if parents:
            fields[vol.Optional("parent")] = _select(parents)
        fields[vol.Optional("prefix")] = TextSelector()
        fields[vol.Required("default", default=DEFAULT_GENERAL)] = _default_select()
        fields[vol.Optional("dedup", default=0)] = NumberSelector(
            NumberSelectorConfig(
                min=0, max=DEDUP_MAX, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="хв",
            )
        )
        fields[vol.Optional("service", default=False)] = BooleanSelector()
        return vol.Schema(fields)

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            cid = user_input["id"].strip()
            existing = {c["id"] for c in self._subs()}
            parent = (user_input.get("parent") or "").strip()
            if not ID_RE.match(cid):
                errors["id"] = "bad_id"
            elif cid in existing:
                errors["id"] = "id_exists"
            elif parent and parent not in self._parents():
                errors["parent"] = "bad_parent"
            else:
                data = {**_category_data({"id": cid}, user_input)}
                return self.async_create_entry(
                    title=sub_title(self.hass, "category", data),
                    data=data,
                    unique_id=cid,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(with_id=True), user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        sub = self._get_reconfigure_subentry()
        cid = sub.data["id"]
        errors: dict[str, str] = {}
        if user_input is not None:
            parent = (user_input.get("parent") or "").strip()
            has_children = any(c.get("parent") == cid for c in self._subs())
            if parent and parent not in self._parents(exclude=cid):
                errors["parent"] = "bad_parent"
            elif parent and has_children:
                errors["parent"] = "has_children"
            else:
                new = _category_data(dict(sub.data), user_input)
                return self.async_update_and_abort(
                    self._get_entry(),
                    sub,
                    title=sub_title(self.hass, "category", new),
                    data=new,
                )
        suggested = user_input or {
            **sub.data,
            "default": sub.data.get("default", DEFAULT_GENERAL),
            "service": sub.data.get("service", False),
            "dedup": sub.data.get("dedup", 0),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(with_id=False, exclude=cid), suggested
            ),
            errors=errors,
        )


class RoutesFlow(OptionsFlow):
    """Маршрути: користувач → (загальний, категорія чи підкатегорія) → режим/канали/резерв."""

    def __init__(self) -> None:
        self._user: str | None = None
        self._target: str | None = None
        self._draft: dict = {}
        self._channels: dict[str, str] = {}  # id -> назва (власні + «Панель HA»)

    def _tree(self) -> dict[str, dict]:
        return category_map(
            [
                dict(s.data)
                for s in self.config_entry.subentries.values()
                if s.subentry_type == "category"
            ]
        )

    def _cat_title(self, tree: dict[str, dict], cid: str) -> str:
        """Назва в списку: «Світло (група)», «Світло › Екстрені», службова з поміткою."""
        title = cat_label(tree, cid)
        if cid in group_ids(tree):
            title += " (група)"
        if tree[cid]["service"]:
            title += " (службова)"
        return title

    def _cats(self) -> dict[str, str]:
        tree = self._tree()
        return {cid: self._cat_title(tree, cid) for cid in ordered_ids(tree)}

    def _summary(self, setting: dict, cid: str | None) -> str:
        """Короткий підсумок налаштування для списку (cid=None: загальний маршрут)."""
        if setting["mode"] == "off":
            return "вимкнено"
        if setting["mode"] == "inherit":
            if cid is None:
                return "лише панель HA"
            tree = self._tree()
            if admin_off(tree, cid):
                return "вимкнено типово"
            return "як у категорії" if tree[cid]["parent"] else "як у загальному"
        names = self._channels
        txt = ", ".join(names.get(c, c) for c in setting["channels"]) or "—"
        if setting["fallback"]:
            txt += " → резерв: " + ", ".join(
                names.get(c, c) for c in setting["fallback"]
            )
        return txt

    def _mode_labels(self, cid: str | None) -> dict[str, str]:
        if cid is None:
            return MODE_LABELS_GENERAL
        tree = self._tree()
        labels = dict(MODE_LABELS)
        if admin_off(tree, cid):
            labels["inherit"] = "Вимкнено типово (адмін); щоб отримувати, обери «Надсилати»"
        elif tree[cid]["parent"]:
            labels["inherit"] = f"Як у категорії «{tree[tree[cid]['parent']]['name']}»"
        return labels

    async def async_step_init(self, user_input=None):
        if self.config_entry.data.get("kind") != KIND_CATEGORIES:
            return self.async_abort(reason="routes_on_categories")
        return self.async_show_menu(step_id="init", menu_options=["routes", "settings"])

    async def async_step_settings(self, user_input=None):
        cur = settings_of(self.hass)
        if user_input is not None:
            new = deepcopy(dict(self.config_entry.options))
            new["settings"] = {"expire_hours": float(user_input["expire_hours"])}
            return self.async_create_entry(data=new)
        schema = vol.Schema(
            {
                vol.Required("expire_hours", default=cur["expire_hours"]): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=720, step=0.1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="год",
                    )
                )
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_routes(self, user_input=None):
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
            if cur.get("quiet"):  # тихі години редагуються на сторінці, тут їх лишаємо
                self._draft["quiet"] = cur["quiet"]
            return await self.async_step_pick()
        return self.async_show_form(
            step_id="routes",
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
            + self._summary(self._draft["default"], None)
        }
        for cid, name in self._cats().items():
            options[cid] = f"{name}: " + self._summary(self._draft["cats"][cid], cid)
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

        modes = self._mode_labels(None if general else self._target)
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
