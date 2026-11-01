"""Диспетчер доставки: маршрути, тихі години, відкладені, дублікати, журнал, expire."""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, CoreState, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import content, quiet, reminders
from .channels import Msg, clear_refs, deliver, mark_done, persistent_dismiss
from .const import COMMON, DOMAIN, FALLBACK_SOURCE
from .helpers import (
    PERSISTENT,
    category_tree,
    channels_of,
    entry_of,
    leads_of,
    persons,
    routes_of,
    settings_of,
)
from .routing import (
    ERR_GROUP,
    ERR_UNKNOWN,
    admin_off,
    apply_prefix,
    check_callable,
    children_of,
    dedup_minutes,
    icon_of,
    label,
    prefix_of,
    resolve_lead,
    resolve_setting,
)

_LOGGER = logging.getLogger(__name__)

PERSISTENT_NAME = "Панель HA"
EVENT_ACTION = "smart_notify_action"
TOKEN_TTL = 7 * 86400
JOURNAL_MAX = 200
STORE_VERSION = 1
RERUN_DELAY = 20  # секунд після старту HA: інтеграції встигають отримати свіжі дані
RERUN_CTX_TTL = 600


class State:
    """Постійний стан: журнал, відкладені, очікування expire, редаговані повідомлення."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.state")
        self.data: dict[str, Any] = {
            "journal": [],
            "queue": [],
            "expiry": [],
            "progress": {},
            "tokens": {},
            "repeats": [],
            "retries": [],
            "reminders": [],
            "sent": {},
            "acks": {},
        }

    async def load(self) -> None:
        loaded = await self._store.async_load()
        if isinstance(loaded, dict):
            for k in self.data:
                if isinstance(loaded.get(k), type(self.data[k])):
                    self.data[k] = loaded[k]

    @callback
    def save(self) -> None:
        self._store.async_delay_save(lambda: self.data, 5)

    @property
    def progress(self) -> dict:
        return self.data["progress"]


class _Run:
    """Контекст одного виклику: що вже пробували й що надіслали."""

    def __init__(self) -> None:
        self.attempted: dict[str, bool] = {}
        self.refs: list[dict] = []
        self.errors: dict[str, str] = {}


def _short(text: str, n: int = 200) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _source_of(channels: dict, cid: str) -> tuple[str, str] | None:
    if cid == PERSISTENT:
        return (PERSISTENT_NAME, FALLBACK_SOURCE)
    ch = channels.get(cid)
    if ch is None:
        _LOGGER.warning("Канал %s більше не існує, пропускаю", cid)
        return None
    return (ch["name"], ch["source"])


class Notifier:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.state = State(hass)
        self._dedup: dict[str, float] = {}
        self._timers: dict[str, Any] = {}
        self._rtimers: dict[str, Any] = {}
        self._unsubs: list = []
        self._unsub_tick = None
        self._unsub_rem = None
        self._unsub_start = None
        self._rerun_ctx: dict[str, float] = {}

    # ------------------------------------------------------------ життєвий цикл

    async def async_start(self) -> None:
        await self.state.load()
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, timedelta(minutes=1)
        )
        now = time.time()
        for rec in list(self.state.data["expiry"]):
            self._schedule_expiry(rec, max(rec["at"] - now, 1))
        self._prune_tokens(now)
        for kind, key in (("repeat", "repeats"), ("retry", "retries")):
            for rec in list(self.state.data[key]):
                self._sched(kind, rec, max(rec["at"] - now, 1))
        self._arm()
        if self.hass.state is CoreState.running:
            _LOGGER.debug("HA уже працює (перезавантаження інтеграції): перезапуск джерел пропущено")
        else:
            self._unsub_start = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )
        bus = self.hass.bus
        self._unsubs = [
            bus.async_listen("mobile_app_notification_action", self._ev_phone),
            bus.async_listen("html5_notification.clicked", self._ev_browser),
            bus.async_listen("telegram_callback", self._ev_telegram),
        ]

    @callback
    def async_stop(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        if self._unsub_rem:
            self._unsub_rem()
            self._unsub_rem = None
        if self._unsub_start:
            self._unsub_start()
            self._unsub_start = None
        for cancel in (*self._timers.values(), *self._rtimers.values(), *self._unsubs):
            cancel()
        self._timers.clear()
        self._rtimers.clear()
        self._unsubs = []

    async def _tick(self, _now: datetime) -> None:
        now = time.time()
        self._prune_tokens(now)
        self._prune_sent(now)
        for cid in [c for c, t in self._rerun_ctx.items() if now - t > RERUN_CTX_TTL]:
            del self._rerun_ctx[cid]
        await self.flush()

    # ------------------------------------------------------------------ журнал

    def _journal(self, entry: dict) -> None:
        j = self.state.data["journal"]
        j.append(entry)
        del j[:-JOURNAL_MAX]
        self.state.save()

    def journal(self) -> list[dict]:
        return list(reversed(self.state.data["journal"]))

    # ------------------------------------------------------------------ expire

    def _schedule_expiry(self, rec: dict, delay: float) -> None:
        async def _fire(_now: datetime) -> None:
            self._timers.pop(rec["id"], None)
            if rec in self.state.data["expiry"]:
                self.state.data["expiry"].remove(rec)
                self.state.save()
            await clear_refs(self.hass, rec["refs"])

        self._timers[rec["id"]] = async_call_later(self.hass, delay, _fire)

    def _register_expiry(self, nid: str, seconds: float, refs: list[dict]) -> None:
        refs = [r for r in refs if r]
        if not refs:
            return
        rec = {"id": f"{nid}:{uuid.uuid4().hex[:4]}", "at": time.time() + seconds, "refs": refs}
        self.state.data["expiry"].append(rec)
        self.state.save()
        self._schedule_expiry(rec, seconds)

    # ------------------------------------------------------------ допоміжне

    def _plan(self, channels: dict, cats: dict, user: str, cat: str):
        route = routes_of(self.hass).get(user)
        s = resolve_setting(cats, cat, route or {})
        if s["mode"] == "off":
            return None
        if user == COMMON and not route:
            # маршрут спільного каналу ще не налаштований: усі спільні канали
            srcs = [(c["name"], c["source"]) for c in channels.values() if c["owner"] == COMMON]
            return (srcs or [(PERSISTENT_NAME, FALLBACK_SOURCE)]), []
        primary = [x for x in (_source_of(channels, c) for c in s["channels"]) if x]
        fallback = [x for x in (_source_of(channels, c) for c in s["fallback"]) if x]
        if not primary and not fallback:
            primary = [(PERSISTENT_NAME, FALLBACK_SOURCE)]
        return primary, fallback

    def _quiet_cfg(self, user: str) -> dict:
        return quiet.norm_quiet(routes_of(self.hass).get(user, {}).get("quiet"))

    def _is_quiet(self, cfg: dict) -> bool:
        state = None
        ent = (cfg.get("entity") or {}).get("entity_id")
        if ent:
            st = self.hass.states.get(ent)
            state = st.state if st else None
        return quiet.is_quiet(cfg, dt_util.now(), state)

    def _dedup_hit(self, user: str, cat: str, window: int, m: Msg) -> bool:
        if window <= 0 or m.std.get("progress") or m.std.get("clear"):
            return False
        key = hashlib.sha1(
            f"{user}|{cat}|{m.title}|{m.message}".encode()
        ).hexdigest()
        now = time.time()
        for k in [k for k, t in self._dedup.items() if now - t > 86400]:
            del self._dedup[k]
        last = self._dedup.get(key)
        if last is not None and now - last < window * 60:
            return True
        self._dedup[key] = now
        return False

    async def _attempt(
        self, run: _Run, name: str, source: str, m: Msg, rec: list[dict]
    ) -> bool:
        if source in run.attempted:
            ok = run.attempted[source]
            rec.append({"channel": name, "status": "sent" if ok else "failed", "shared": True})
            return ok
        try:
            refs = await deliver(self.hass, self.state, source, m)
            run.refs.extend(refs)
            self._track(m, refs)
            run.attempted[source] = True
            rec.append({"channel": name, "status": "sent"})
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Не доставлено: user=%s канал=%s", m.user, name)
            run.attempted[source] = False
            rec.append({"channel": name, "status": "failed", "error": _short(str(err), 120)})
        return run.attempted[source]

    async def _deliver_user(
        self,
        run: _Run,
        channels: dict,
        cats: dict,
        user: str,
        m: Msg,
        last_resort: bool = True,
    ) -> dict:
        """Доставка одній людині за її маршрутом (без перевірки тихих годин).

        ok: чи спрацював хоч один справжній канал (без аварійної панелі HA);
        persist: посилання на аварійне персистентне сповіщення, якщо його створено.
        """
        plan = self._plan(channels, cats, user, m.category)
        if plan is None:
            return {"user": user, "status": "off", "channels": [], "ok": True, "persist": []}
        primary, fallback = plan
        rec: list[dict] = []
        ok = False
        for name, source in primary:
            if await self._attempt(run, name, source, m, rec):
                ok = True
        if not ok:
            for name, source in fallback:
                if await self._attempt(run, name, source, m, rec):
                    ok = True
                    break
        persist: list[dict] = []
        if not ok and last_resort and FALLBACK_SOURCE not in run.attempted:
            _LOGGER.error("Жоден канал не спрацював для %s, надсилаю в панель HA", user)
            before = len(run.refs)
            await self._attempt(run, PERSISTENT_NAME, FALLBACK_SOURCE, m, rec)
            persist = [r for r in run.refs[before:] if r.get("kind") == "persistent"]
        return {
            "user": user,
            "status": "sent" if any(r["status"] == "sent" for r in rec) else "failed",
            "channels": rec,
            "ok": ok,
            "persist": persist,
        }

    async def _deliver_common(self, run: _Run, channels: dict, cats: dict, m: Msg) -> dict:
        """Спільний канал: маршрут «common» за категоріями (без тихих годин і аварійної панелі)."""
        res = await self._deliver_user(run, channels, cats, COMMON, m, last_resort=False)
        res["persist"] = []
        return res

    async def _deliver_target(
        self, run: _Run, channels: dict, cats: dict, user: str, m: Msg, last_resort: bool = True
    ) -> dict:
        if user == COMMON:
            return await self._deliver_common(run, channels, cats, m)
        return await self._deliver_user(run, channels, cats, user, m, last_resort)

    # ------------------------------------------- кнопки, повтор, повтор доставки

    def _prune_tokens(self, now: float) -> None:
        tokens = self.state.data["tokens"]
        old = [t for t, v in tokens.items() if now - v.get("ts", 0) > TOKEN_TTL]
        acks = self.state.data["acks"]
        for nid in [n for n, v in acks.items() if now - v.get("ts", 0) > TOKEN_TTL]:
            del acks[nid]
        for t in old:
            del tokens[t]
        if old:
            self.state.save()

    def _prepare(self, m: Msg) -> None:
        """Токен для кнопок; для повтору без кнопок додається «Підтвердити»."""
        std = m.std
        if std.get("repeat") is not None and not any(a.get("id") for a in std.get("actions", [])):
            std["actions"] = [*std.get("actions", []), {"title": "Підтвердити", "id": "ack"}]
        if any(a.get("id") for a in std.get("actions", [])):
            m.token = uuid.uuid4().hex[: content.TOKEN_LEN]
            self.state.data["tokens"][m.token] = {
                "user": m.user,
                "category": m.category,
                "tag": m.tag,
                "id": m.id,
                "ts": time.time(),
            }
            self.state.save()

    def _track(self, m: Msg, refs: list[dict]) -> None:
        """Запам'ятовує надіслане з кнопками, щоб після підтвердження зняти його в усіх."""
        if not m.token or m.std.get("clear"):
            return
        refs = [r for r in refs if r and r.get("kind") in ("phone", "telegram", "persistent")]
        acks = self.state.data["acks"]
        rec = acks.get(m.id)
        if rec is None:
            rec = acks[m.id] = {
                "ts": time.time(),
                "scope": m.std.get("ack", "all"),
                "titles": {a["id"]: a["title"] for a in m.std.get("actions", []) if a.get("id")},
                "users": {},
            }
        if refs:
            rec["users"].setdefault(m.user, []).extend(refs)
        self.state.save()

    def _record(self, m: Msg) -> dict:
        return {
            "id": uuid.uuid4().hex[:8],
            "nid": m.id,
            "user": m.user,
            "category": m.category,
            "title": m.title,
            "message": m.message,
            "std": dict(m.std),
            "extra": dict(m.extra),
            "raw": dict(m.raw),
            "token": m.token,
            "count": 0,
        }

    def _msg_from(self, rec: dict, cats: dict) -> Msg | None:
        if rec["category"] not in cats:
            return None
        m = self._make_msg(
            rec["nid"], cats, rec["category"], rec["title"], rec["message"],
            rec["std"], rec["extra"], rec["raw"], rec["user"],
        )
        m.token = rec.get("token", "")
        return m

    def _sched(self, kind: str, rec: dict, delay: float) -> None:
        key = f"{kind}:{rec['id']}"
        if key in self._rtimers:
            self._rtimers.pop(key)()

        async def _fire(_now: datetime) -> None:
            self._rtimers.pop(key, None)
            if kind == "repeat":
                await self._repeat_fire(rec)
            else:
                await self._retry_fire(rec)

        self._rtimers[key] = async_call_later(self.hass, delay, _fire)

    def _drop(self, kind: str, rec: dict) -> None:
        lst = self.state.data["repeats" if kind == "repeat" else "retries"]
        if rec in lst:
            lst.remove(rec)
            self.state.save()
        cancel = self._rtimers.pop(f"{kind}:{rec['id']}", None)
        if cancel:
            cancel()

    def _start_repeat(self, m: Msg) -> None:
        rep = m.std.get("repeat")
        if rep is None:
            return
        st = settings_of(self.hass)
        every = rep.get("every") or st["repeat_every_min"] * 60
        mx = rep.get("max") or st["repeat_max"]
        rec = self._record(m)
        now = time.time()
        rec.update({"every": every, "max": mx, "at": now + every})
        if m.std.get("expire"):
            rec["expire_at"] = now + m.std["expire"]
        self.state.data["repeats"].append(rec)
        self.state.save()
        self._sched("repeat", rec, every)

    def _start_retry(self, m: Msg, persist: list[dict]) -> None:
        st = settings_of(self.hass)
        every = st["retry_every_min"] * 60
        rec = self._record(m)
        now = time.time()
        rec.update(
            {
                "every": every,
                "max": st["retry_max"],
                "at": now + every,
                "attempt": 0,
                "persist": persist,
            }
        )
        if m.std.get("expire"):
            rec["expire_at"] = now + m.std["expire"]
        self.state.data["retries"].append(rec)
        self.state.save()
        self._sched("retry", rec, every)

    def _after(self, m: Msg, res: dict) -> None:
        """Після доставки: повтор до підтвердження або повтор доставки при збої."""
        if res.get("status") in ("off",):
            return
        if res.get("ok"):
            self._start_repeat(m)
        elif m.std.get("clear") is not True:
            self._start_retry(m, res.get("persist", []))

    async def _repeat_fire(self, rec: dict) -> None:
        if rec not in self.state.data["repeats"]:
            return
        now = time.time()
        cats = category_tree(self.hass)
        m = self._msg_from(rec, cats)
        if m is None or (rec.get("expire_at") and now > rec["expire_at"]):
            self._drop("repeat", rec)
            return
        if rec["user"] != COMMON:
            cfg = self._quiet_cfg(rec["user"])
            if cfg["enabled"] and self._is_quiet(cfg) and quiet.behavior_for(cfg, m.priority) != "pass":
                self._sched("repeat", rec, rec["every"])  # тихі години: не рахуємо, чекаємо
                return
        rec["count"] += 1
        m.note = f"повтор {rec['count']}"
        await self._deliver_target(_Run(), channels_of(self.hass), cats, rec["user"], m, False)
        if rec["count"] >= rec["max"]:
            self._drop("repeat", rec)
        else:
            rec["at"] = now + rec["every"]
            self.state.save()
            self._sched("repeat", rec, rec["every"])

    async def _retry_fire(self, rec: dict) -> None:
        if rec not in self.state.data["retries"]:
            return
        now = time.time()
        cats = category_tree(self.hass)
        m = self._msg_from(rec, cats)
        if m is None or (rec.get("expire_at") and now > rec["expire_at"]):
            self._drop("retry", rec)
            return
        rec["attempt"] += 1
        res = await self._deliver_target(_Run(), channels_of(self.hass), cats, rec["user"], m, False)
        if res.get("ok"):
            for ref in rec.get("persist", []):
                try:
                    await persistent_dismiss(self.hass, ref)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("Не вдалося закрити аварійне сповіщення", exc_info=True)
            self._drop("retry", rec)
            self._journal(
                {
                    "ts": now, "id": rec["nid"], "category": rec["category"],
                    "title": _short(rec["title"] or "", 80),
                    "message": _short(rec["message"], 200),
                    "priority": rec["std"].get("priority", "normal"),
                    "test": False, "retry": rec["attempt"],
                    "results": [{"user": rec["user"], "status": "retry_sent", "channels": res["channels"]}],
                }
            )
            m.note = ""
            self._start_repeat(m)
        elif rec["attempt"] >= rec["max"]:
            _LOGGER.error(
                "Не вдалося доставити %s для %s після %s повторів", rec["category"], rec["user"], rec["attempt"]
            )
            self._drop("retry", rec)
        else:
            rec["at"] = now + rec["every"]
            self.state.save()
            self._sched("retry", rec, rec["every"])

    @callback
    def _ev_phone(self, event: Any) -> None:
        self._on_action("phone", event.data.get("action"), event)

    @callback
    def _ev_browser(self, event: Any) -> None:
        self._on_action("browser", event.data.get("action"), event)

    @callback
    def _ev_telegram(self, event: Any) -> None:
        self._on_action("telegram", event.data.get("data"), event)

    def _on_action(self, channel: str, raw: Any, event: Any) -> None:
        dec = content.decode_action(raw)
        if dec is None:
            return
        token, action_id = dec
        info = self.state.data["tokens"].get(token)
        if info is None:
            return
        user = info["user"]
        nid = info["id"]
        ack = self.state.data["acks"].get(nid) or {}
        scope = ack.get("scope", "all")
        if scope == "all":
            # підтвердження будь-ким знімає повтори й повтори доставки в усіх отримувачів
            for rec in [r for r in self.state.data["repeats"] if r.get("nid") == nid]:
                self._drop("repeat", rec)
            for rec in [r for r in self.state.data["retries"] if r.get("nid") == nid]:
                self._drop("retry", rec)
            refs = [r for lst in ack.get("users", {}).values() for r in lst]
        else:
            for rec in [r for r in self.state.data["repeats"] if r.get("token") == token]:
                self._drop("repeat", rec)
            refs = list(ack.get("users", {}).get(user, []))
        if ack and not ack.get("done"):
            if scope == "all":
                ack["done"] = True  # «self»: інші ще можуть підтвердити своє
            title = ack.get("titles", {}).get(action_id) or "Підтверджено"
            who = self._person_name(user)
            hhmm = dt_util.now().strftime("%H:%M")
            mark = f"✅ {title}" + (f" · {who}" if who else "") + f" · {hhmm}"
            if scope == "all":
                ack["users"] = {}
            else:
                ack["users"].pop(user, None)
            self.state.save()
            if refs:
                self.hass.async_create_task(mark_done(self.hass, refs, mark))
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "id": action_id,
                "person": user,
                "category": info["category"],
                "tag": info.get("tag"),
                "channel": channel,
                "notification_id": info["id"],
                "scope": scope,
            },
        )
        if channel == "telegram" and event.data.get("id"):
            self.hass.async_create_task(self._tg_answer(event.data["id"]))

    async def _tg_answer(self, callback_id: str) -> None:
        if not self.hass.services.has_service("telegram_bot", "answer_callback_query"):
            return
        try:
            await self.hass.services.async_call(
                "telegram_bot",
                "answer_callback_query",
                {"message": "Прийнято", "callback_query_id": callback_id},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Не вдалося відповісти на callback", exc_info=True)

    def _make_msg(
        self, nid: str, cats: dict, cat: str, title, message, std, extra, raw, user: str
    ) -> Msg:
        return Msg(
            id=nid,
            message=message,
            title=title,
            std=dict(std),
            extra=dict(extra),
            raw=dict(raw),
            category=cat,
            cat_name=cats[cat]["name"],
            icon=icon_of(cats, cat),
            user=user,
        )

    # ------------------------------------------------------------ пропуски

    def _state_text(self, entity_id: str) -> str | None:
        st = self.hass.states.get(entity_id)
        if st is None or st.state in ("unknown", "unavailable"):
            return None
        unit = st.attributes.get("unit_of_measurement")
        return f"{st.state} {unit}" if unit else st.state

    def _person_name(self, user: str) -> str:
        if user == COMMON:
            return ""
        return persons(self.hass).get(user, user)

    def _render(
        self, title, message, user: str, std: dict, event: datetime | None, variables: dict
    ) -> tuple[str | None, str]:
        """Заповнює пропуски в заголовку й тексті для одного отримувача."""
        if not (reminders.has_placeholders(title) or reminders.has_placeholders(message)):
            return title, message
        kw = {
            "now": dt_util.utcnow(),
            "tz": dt_util.get_default_time_zone(),
            "event": event,
            "variables": variables,
            "person": self._person_name(user),
            "state_of": self._state_text,
            "html": std.get("format") == "html",
        }
        return reminders.render(title, **kw), reminders.render(message, **kw) or ""

    # ------------------------------------------------------------ спільний конвеєр

    async def _pipeline(self, run: _Run, channels: dict, cats: dict, user: str, m: Msg) -> dict:
        """Тихі години, маршрут, канали, повтори для однієї людини."""
        cfg = self._quiet_cfg(user)
        action = "pass"
        if cfg["enabled"] and self._is_quiet(cfg):
            action = quiet.behavior_for(cfg, m.priority)
        if action == "skip":
            return {"user": user, "status": "quiet_skipped", "channels": []}
        if action == "defer":
            if self._plan(channels, cats, user, m.category) is None:
                return {"user": user, "status": "off", "channels": []}
            self._enqueue(m.id, user, m.category, m.title, m.message, m.std, m.extra, m.raw)
            return {"user": user, "status": "deferred", "channels": []}
        if action == "silent":
            m.silent = True
        self._prepare(m)
        res = await self._deliver_user(run, channels, cats, user, m)
        self._after(m, res)
        return res

    def _presence_ok(self, user: str, presence: str) -> bool:
        if user == COMMON or presence not in ("home", "away"):
            return True
        st = self.hass.states.get(f"person.{user}")
        at_home = st is not None and st.state == "home"
        return at_home if presence == "home" else not at_home

    # ------------------------------------------------------------ головна дія

    async def send(self, data: dict, *, test: bool = False, context: Context | None = None) -> dict:
        hass = self.hass
        cat = slugify(data["category"])
        cats = category_tree(hass)
        err = check_callable(cats, cat)
        if err == ERR_UNKNOWN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=ERR_UNKNOWN,
                translation_placeholders={"category": cat},
            )
        if err == ERR_GROUP:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=ERR_GROUP,
                translation_placeholders={
                    "category": cat,
                    "children": ", ".join(children_of(cats, cat)),
                },
            )
        try:
            event = reminders.parse_event_time(data.get("event_time"), dt_util.get_default_time_zone())
        except (TypeError, ValueError, OverflowError, OSError):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_event_time",
                translation_placeholders={"value": str(data.get("event_time"))},
            ) from None

        nid = uuid.uuid4().hex[:8]
        if self._is_rerun(context) and event is None:
            # перезапуск після старту HA: звичайні сповіщення не дублюємо
            return {
                "id": nid,
                "delivered": False,
                "results": [{"user": "*", "status": "restart_skipped", "channels": []}],
            }

        channels = channels_of(hass)
        raw = dict(data["data"]) if isinstance(data.get("data"), dict) else {}
        variables = reminders.norm_vars(raw.pop("vars", None))
        std, extra = content.parse_data(raw)
        payload = apply_prefix(
            prefix_of(cats, cat),
            {k: data[k] for k in ("title", "message") if k in data},
        )
        title, message = payload.get("title"), data["message"]
        if event is None and std.get("expire") and not std.get("tag"):
            std["tag"] = f"sn_{nid}"  # без тегу телефон не можна очистити за expire
        window = dedup_minutes(cats, cat)

        # --- кому надсилаємо
        users = [slugify(u.removeprefix("person.")) for u in data.get("user", [])]
        exclude = {slugify(u.removeprefix("person.")) for u in data.get("exclude", [])}
        presence = data.get("presence", "any")
        run = _Run()
        results: list[dict] = []

        # «common» окремий отримувач (спільні канали); «all» і порожній список: усі люди
        want_common = COMMON in users
        users = [u for u in users if u != COMMON]
        if "all" in users or not (users or want_common):
            users = list(persons(hass))

        if std.get("clear") and std.get("tag"):
            self._cancel_reminders(std["tag"], set(users) | ({COMMON} if want_common else set()))

        if event is not None:
            targets = [u for u in users if u not in exclude]
            results.extend(
                {"user": u, "status": "filtered", "channels": []} for u in users if u in exclude
            )
            if want_common:
                targets.append(COMMON)
            for u in targets:
                results.append(
                    await self._remind(
                        u, nid, cats, cat, title, message, std, extra, raw, variables, event, presence
                    )
                )
        else:
            for u in users:
                if u in exclude or not self._presence_ok(u, presence):
                    results.append({"user": u, "status": "filtered", "channels": []})
                    continue
                t, msg = self._render(title, message, u, std, None, variables)
                m = self._make_msg(nid, cats, cat, t, msg, std, extra, raw, u)
                if not test and self._dedup_hit(u, cat, window, m):
                    results.append({"user": u, "status": "duplicate", "channels": []})
                    continue
                results.append(await self._pipeline(run, channels, cats, u, m))

            if want_common:
                if admin_off(cats, cat):
                    results.append({"user": COMMON, "status": "off", "channels": []})
                else:
                    t, msg = self._render(title, message, COMMON, std, None, variables)
                    m = self._make_msg(nid, cats, cat, t, msg, std, extra, raw, COMMON)
                    self._prepare(m)
                    res = await self._deliver_common(run, channels, cats, m)
                    self._after(m, res)
                    results.append(res)

            if std.get("expire") and run.refs:
                self._register_expiry(nid, std["expire"], run.refs)

        delivered = any(
            r["status"] == "sent" or r.get("sent_now") for r in results
        )
        entry = {
            "ts": time.time(),
            "id": nid,
            "category": cat,
            "title": _short(title or "", 80),
            "message": _short(message, 200),
            "priority": std["priority"],
            "test": test,
            "results": [_journal_result(r) for r in results],
        }
        if event is not None:
            entry["event_time"] = event.timestamp()
        self._journal(entry)
        for r in results:
            r.pop("ok", None)
            r.pop("persist", None)
        return {"id": nid, "delivered": delivered, "results": results}

    # ------------------------------------------------------------ нагадування

    def _is_rerun(self, context: Context | None) -> bool:
        if context is None:
            return False
        return context.id in self._rerun_ctx or (
            context.parent_id is not None and context.parent_id in self._rerun_ctx
        )

    def _lead_for(self, cats: dict, user: str, cat: str):
        return resolve_lead(cats, cat, leads_of(self.hass, user))

    async def _remind(
        self, user, nid, cats, cat, title, message, std, extra, raw, variables, event, presence
    ) -> dict:
        """Виклик із часом події для одного отримувача: одразу, запланувати або пропустити."""
        if user == COMMON and admin_off(cats, cat):
            return {"user": user, "status": "off", "channels": []}
        lead = self._lead_for(cats, user, cat)
        if lead == reminders.LEAD_OFF:
            return {"user": user, "status": "reminder_off", "channels": []}
        now = dt_util.utcnow()
        if event <= now:
            return {"user": user, "status": "expired", "channels": []}

        key = f"{user}|{std.get('tag') or cat}"
        fp = reminders.fingerprint(event, title, message, variables)
        rec = {
            "id": uuid.uuid4().hex[:8],
            "nid": nid,
            "key": key,
            "fp": fp,
            "user": user,
            "category": cat,
            "tag": std.get("tag"),
            "title": title,
            "message": message,
            "std": dict(std),
            "extra": dict(extra),
            "raw": dict(raw),
            "vars": dict(variables),
            "event": event.timestamp(),
            "presence": presence,
        }
        sent = self.state.data["sent"].setdefault(key, {})
        sent["until"] = event.timestamp() + 86400
        out: dict = {"user": user, "status": "scheduled", "channels": []}

        if lead is None:  # не налаштовано: одразу і в момент події
            if sent.get("now") != fp:
                res = await self._deliver_rec({**rec, "phase": "now"}, cats)
                out["sent_now"] = res.get("status") == "sent"
                out["now_status"] = res.get("status")
                out["channels"] = res.get("channels", [])
            else:
                out["now_status"] = "duplicate"

        due = reminders.due_of(event, lead if isinstance(lead, int) else None)
        rec["due"] = due.timestamp()
        self._drop_key(key)
        if sent.get("due") == fp:
            out["status"] = "duplicate"
            self.state.save()
            return out
        if due <= now:
            res = await self._deliver_rec({**rec, "phase": "due"}, cats)
            res = dict(res)
            res.pop("ok", None)
            res.pop("persist", None)
            return {**res, "user": user}
        self.state.data["reminders"].append(rec)
        self.state.save()
        self._arm()
        out["at"] = dt_util.as_local(due).isoformat()
        return out

    def _drop_key(self, key: str) -> None:
        lst = self.state.data["reminders"]
        keep = [r for r in lst if r["key"] != key]
        if len(keep) != len(lst):
            lst[:] = keep
            self.state.save()

    def _cancel_reminders(self, tag: str, users: set[str]) -> None:
        lst = self.state.data["reminders"]
        keep = [r for r in lst if not (r.get("tag") == tag and r["user"] in users)]
        if len(keep) != len(lst):
            lst[:] = keep
            self.state.save()
            self._arm()

    def cancel_reminder(self, rid: str, users: set[str] | None = None) -> bool:
        lst = self.state.data["reminders"]
        for r in lst:
            if r["id"] == rid and (users is None or r["user"] in users):
                lst.remove(r)
                self.state.save()
                self._arm()
                return True
        return False

    def reminders_for(self, users) -> list[dict]:
        users = set(users)
        return sorted(
            (r for r in self.state.data["reminders"] if r["user"] in users),
            key=lambda r: r["due"],
        )

    def recompute(self, user: str) -> None:
        """«Нагадувати за» людини змінилось: перераховуємо її заплановані нагадування."""
        cats = category_tree(self.hass)
        lst = self.state.data["reminders"]
        changed = False
        for r in list(lst):
            if r["user"] != user:
                continue
            if r["category"] not in cats:
                lst.remove(r)
                changed = True
                continue
            lead = self._lead_for(cats, user, r["category"])
            if lead == reminders.LEAD_OFF:
                lst.remove(r)
                changed = True
                continue
            event = datetime.fromtimestamp(r["event"], dt_util.UTC)
            due = reminders.due_of(event, lead if isinstance(lead, int) else None).timestamp()
            if due != r["due"]:
                r["due"] = due
                changed = True
        if changed:
            self.state.save()
        self._arm()

    def _prune_sent(self, now: float) -> None:
        sent = self.state.data["sent"]
        old = [k for k, v in sent.items() if v.get("until", 0) < now]
        for k in old:
            del sent[k]
        if old:
            self.state.save()

    @callback
    def _arm(self) -> None:
        """Один таймер на найближче нагадування."""
        if self._unsub_rem:
            self._unsub_rem()
            self._unsub_rem = None
        lst = self.state.data["reminders"]
        if not lst:
            return
        at = min(r["due"] for r in lst)
        when = datetime.fromtimestamp(max(at, time.time() + 1), dt_util.UTC)
        self._unsub_rem = async_track_point_in_utc_time(self.hass, self._fire, when)

    async def _fire(self, _now: datetime) -> None:
        self._unsub_rem = None
        now = time.time()
        lst = self.state.data["reminders"]
        due = sorted((r for r in lst if r["due"] <= now + 1), key=lambda r: r["due"])
        for r in due:
            lst.remove(r)
        if due:
            self.state.save()
        cats = category_tree(self.hass)
        for r in due:
            try:
                await self._deliver_rec({**r, "phase": "due"}, cats, journal=True)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Не вдалося надіслати нагадування %s", r.get("id"))
        self._arm()

    async def _deliver_rec(self, rec: dict, cats: dict, journal: bool = False) -> dict:
        """Надсилає нагадування (одразу або заплановане) одному отримувачу."""
        user = rec["user"]
        now_dt = dt_util.utcnow()
        now = now_dt.timestamp()
        event = datetime.fromtimestamp(rec["event"], dt_util.UTC)
        res: dict
        if rec["category"] not in cats:
            res = {"user": user, "status": "off", "channels": []}
        elif now > rec["event"] + reminders.FIRE_GRACE:
            res = {"user": user, "status": "expired", "channels": []}
        elif not self._presence_ok(user, rec.get("presence", "any")):
            res = {"user": user, "status": "filtered", "channels": []}
        else:
            std = dict(rec["std"])
            if not std.get("expire") and rec["event"] - now > 60:
                std["expire"] = rec["event"] - now  # неактуальне, коли подія настала
            if std.get("expire") and not std.get("tag"):
                std["tag"] = f"sn_{rec['nid']}"
            t, msg = self._render(rec["title"], rec["message"], user, std, event, rec.get("vars") or {})
            m = self._make_msg(rec["nid"], cats, rec["category"], t, msg, std, rec["extra"], rec["raw"], user)
            run = _Run()
            if user == COMMON:
                self._prepare(m)
                res = await self._deliver_common(run, channels_of(self.hass), cats, m)
                self._after(m, res)
            else:
                res = await self._pipeline(run, channels_of(self.hass), cats, user, m)
            if std.get("expire") and run.refs:
                self._register_expiry(rec["nid"], std["expire"], run.refs)
        if res.get("status") in ("sent", "failed", "deferred", "quiet_skipped", "filtered"):
            sent = self.state.data["sent"].setdefault(rec["key"], {})
            sent[rec.get("phase", "due")] = rec["fp"]
            sent["until"] = rec["event"] + 86400
            self.state.save()
        if journal:
            self._journal(
                {
                    "ts": now,
                    "id": rec["nid"],
                    "category": rec["category"],
                    "title": _short(rec["title"] or "", 80),
                    "message": _short(rec["message"], 200),
                    "priority": rec["std"].get("priority", "normal"),
                    "test": False,
                    "reminder": True,
                    "event_time": rec["event"],
                    "results": [_journal_result(res)],
                }
            )
        return res

    # ------------------------------------------------------------ перезапуск джерел

    @callback
    def _on_ha_started(self, _event: Any) -> None:
        self._unsub_start = None
        if not settings_of(self.hass)["rerun_on_start"]:
            return

        async def _later(_now: datetime) -> None:
            await self.rerun_sources()

        self._unsubs.append(async_call_later(self.hass, RERUN_DELAY, _later))

    def _sources(self) -> list[tuple[str, str]]:
        """(домен, entity_id) автоматизацій і скриптів, що ставлять нагадування."""
        out: list[tuple[str, str]] = []
        for domain in ("automation", "script"):
            comp = self.hass.data.get(domain)
            entities = getattr(comp, "entities", None)
            if entities is None:
                _LOGGER.warning("Не вдалося прочитати %s: перезапуск після старту недоступний", domain)
                continue
            for ent in entities:
                cfg = getattr(ent, "raw_config", None)
                if cfg is None or not reminders.uses_event_time(cfg):
                    continue
                if domain == "automation" and not getattr(ent, "is_on", True):
                    continue
                if domain == "script" and (getattr(ent, "fields", None) or (cfg or {}).get("fields")):
                    _LOGGER.info("Скрипт %s має поля (fields), не перезапускаю", ent.entity_id)
                    continue
                out.append((domain, ent.entity_id))
        return out

    async def rerun_sources(self) -> list[str]:
        """Запускає автоматизації й скрипти з event_time, щоб підхопити зміни за час простою."""
        try:
            sources = self._sources()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Не вдалося знайти джерела нагадувань", exc_info=True)
            return []
        done: list[str] = []
        for domain, eid in sources:
            ctx = Context()
            self._rerun_ctx[ctx.id] = time.time()
            service, data = (
                ("trigger", {"entity_id": eid, "skip_condition": False})
                if domain == "automation"
                else ("turn_on", {"entity_id": eid})
            )
            try:
                await self.hass.services.async_call(domain, service, data, blocking=False, context=ctx)
                done.append(eid)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Не вдалося перезапустити %s", eid, exc_info=True)
        if done:
            _LOGGER.info("Перезапущено джерела нагадувань: %s", ", ".join(done))
        return done

    # ------------------------------------------------------------ відкладені

    def _enqueue(self, nid, user, cat, title, message, std, extra, raw) -> None:
        now = time.time()
        hours = settings_of(self.hass)["expire_hours"]
        lifetime = std.get("expire") or hours * 3600
        self.state.data["queue"].append(
            {
                "id": nid,
                "user": user,
                "category": cat,
                "title": title,
                "message": message,
                "std": std,
                "extra": extra,
                "raw": raw,
                "ts": now,
                "expire_at": now + lifetime,
            }
        )
        self.state.save()

    async def flush(self) -> None:
        """Віддає відкладене тим, у кого тихі години вже закінчилися."""
        queue = self.state.data["queue"]
        if not queue:
            return
        now = time.time()
        cats = category_tree(self.hass)
        channels = channels_of(self.hass)

        for user in sorted({q["user"] for q in queue}):
            if self._is_quiet(self._quiet_cfg(user)):
                continue
            mine = [q for q in queue if q["user"] == user]
            for q in mine:
                queue.remove(q)
            live = []
            for q in mine:
                if q["expire_at"] < now:
                    self._journal(
                        {
                            "ts": now,
                            "id": q["id"],
                            "category": q["category"],
                            "title": _short(q["title"] or "", 80),
                            "message": _short(q["message"], 200),
                            "priority": q["std"].get("priority", "normal"),
                            "test": False,
                            "results": [{"user": user, "status": "expired", "channels": []}],
                        }
                    )
                elif q["category"] in cats:
                    live.append(q)
            self.state.save()

            singles, digests = quiet.plan_flush(live)
            for q in singles:
                run = _Run()
                std = dict(q["std"])
                if std.get("expire"):
                    std["expire"] = max(q["expire_at"] - now, 1)
                    std.setdefault("tag", f"sn_{q['id']}")
                m = self._make_msg(
                    q["id"], cats, q["category"], q["title"], q["message"], std,
                    q["extra"], q["raw"], user,
                )
                m.note = dt_util.as_local(datetime.fromtimestamp(q["ts"], dt_util.UTC)).strftime("%H:%M")
                self._prepare(m)
                res = await self._deliver_user(run, channels, cats, user, m)
                self._after(m, res)
                self._journal_flush(q, res)
            for cat, items in digests.items():
                run = _Run()
                lines = []
                for q in items:
                    hhmm = dt_util.as_local(
                        datetime.fromtimestamp(q["ts"], dt_util.UTC)
                    ).strftime("%H:%M")
                    text = q["title"] or q["message"]
                    if q["title"] and q["message"]:
                        text = f"{q['title']}: {q['message']}"
                    lines.append(f"{hhmm} {_short(text, 100)}")
                std, extra = content.parse_data({})
                m = self._make_msg(
                    uuid.uuid4().hex[:8], cats, cat, f"{cats[cat]['name']}: {len(items)}",
                    "\n".join(lines), std, extra, {}, user,
                )
                res = await self._deliver_user(run, channels, cats, user, m)
                self._journal_flush({**items[0], "title": m.title, "message": m.message}, res)

    def _journal_flush(self, q: dict, res: dict) -> None:
        self._journal(
            {
                "ts": time.time(),
                "id": q["id"],
                "category": q["category"],
                "title": _short(q["title"] or "", 80),
                "message": _short(q["message"], 200),
                "priority": q["std"].get("priority", "normal") if "std" in q else "normal",
                "test": False,
                "deferred": True,
                "results": [res],
            }
        )

    def queue_count(self, user: str) -> int:
        return sum(1 for q in self.state.data["queue"] if q["user"] == user)


def _journal_result(r: dict) -> dict:
    out = {
        "user": r["user"],
        "status": r["status"],
        "channels": [{k: v for k, v in c.items() if k != "shared"} for c in r.get("channels", [])],
    }
    for k in ("at", "sent_now", "now_status"):
        if k in r:
            out[k] = r[k]
    return out


def notifier_of(hass: HomeAssistant) -> Notifier:
    return hass.data[DOMAIN]["notifier"]


__all__ = ["Notifier", "notifier_of", "entry_of", "label"]
