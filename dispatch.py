"""Диспетчер доставки: маршрути, тихі години, відкладені, дублікати, журнал, expire."""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import content, quiet
from .channels import Msg, clear_refs, deliver
from .const import COMMON, DOMAIN, FALLBACK_SOURCE
from .helpers import (
    PERSISTENT,
    category_tree,
    channels_of,
    entry_of,
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
    resolve_setting,
)

_LOGGER = logging.getLogger(__name__)

PERSISTENT_NAME = "Панель HA"
JOURNAL_MAX = 200
STORE_VERSION = 1


class State:
    """Постійний стан: журнал, відкладені, очікування expire, редаговані повідомлення."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.state")
        self.data: dict[str, Any] = {"journal": [], "queue": [], "expiry": [], "progress": {}}

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
        self._unsub_tick = None

    # ------------------------------------------------------------ життєвий цикл

    async def async_start(self) -> None:
        await self.state.load()
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, timedelta(minutes=1)
        )
        now = time.time()
        for rec in list(self.state.data["expiry"]):
            self._schedule_expiry(rec, max(rec["at"] - now, 1))

    @callback
    def async_stop(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    async def _tick(self, _now: datetime) -> None:
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
        s = resolve_setting(cats, cat, routes_of(self.hass).get(user, {}))
        if s["mode"] == "off":
            return None
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
            run.attempted[source] = True
            rec.append({"channel": name, "status": "sent"})
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Не доставлено: user=%s канал=%s", m.user, name)
            run.attempted[source] = False
            rec.append({"channel": name, "status": "failed", "error": _short(str(err), 120)})
        return run.attempted[source]

    async def _deliver_user(
        self, run: _Run, channels: dict, cats: dict, user: str, m: Msg
    ) -> dict:
        """Доставка одній людині за її маршрутом (без перевірки тихих годин)."""
        plan = self._plan(channels, cats, user, m.category)
        if plan is None:
            return {"user": user, "status": "off", "channels": []}
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
        if not ok and FALLBACK_SOURCE not in run.attempted:
            _LOGGER.error("Жоден канал не спрацював для %s, надсилаю в панель HA", user)
            await self._attempt(run, PERSISTENT_NAME, FALLBACK_SOURCE, m, rec)
        return {
            "user": user,
            "status": "sent" if any(r["status"] == "sent" for r in rec) else "failed",
            "channels": rec,
        }

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

    # ------------------------------------------------------------ головна дія

    async def send(self, data: dict, *, test: bool = False) -> dict:
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

        channels = channels_of(hass)
        raw = data.get("data") if isinstance(data.get("data"), dict) else {}
        std, extra = content.parse_data(raw)
        payload = apply_prefix(
            prefix_of(cats, cat),
            {k: data[k] for k in ("title", "message") if k in data},
        )
        title, message = payload.get("title"), data["message"]
        nid = uuid.uuid4().hex[:8]
        if std.get("expire") and not std.get("tag"):
            std["tag"] = f"sn_{nid}"  # без тегу телефон не можна очистити за expire
        window = dedup_minutes(cats, cat)

        # --- кому надсилаємо
        users = [slugify(u.removeprefix("person.")) for u in data.get("user", [])]
        exclude = {slugify(u.removeprefix("person.")) for u in data.get("exclude", [])}
        presence = data.get("presence", "any")
        run = _Run()
        results: list[dict] = []

        def person_ok(u: str) -> bool:
            if u in exclude:
                return False
            if presence in ("home", "away"):
                st = hass.states.get(f"person.{u}")
                at_home = st is not None and st.state == "home"
                return at_home if presence == "home" else not at_home
            return True

        common_path = False
        if "all" in users or not users:
            common = [c for c in channels.values() if c["owner"] == COMMON]
            if not users and common:
                common_path = True
            else:
                users = list(persons(hass))

        if common_path:
            if admin_off(cats, cat):
                results.append({"user": COMMON, "status": "off", "channels": []})
            else:
                m = self._make_msg(nid, cats, cat, title, message, std, extra, raw, COMMON)
                rec: list[dict] = []
                for c in (c for c in channels.values() if c["owner"] == COMMON):
                    await self._attempt(run, c["name"], c["source"], m, rec)
                results.append(
                    {
                        "user": COMMON,
                        "status": "sent" if any(r["status"] == "sent" for r in rec) else "failed",
                        "channels": rec,
                    }
                )
        else:
            for u in users:
                if not person_ok(u):
                    results.append({"user": u, "status": "filtered", "channels": []})
                    continue
                m = self._make_msg(nid, cats, cat, title, message, std, extra, raw, u)
                if not test and self._dedup_hit(u, cat, window, m):
                    results.append({"user": u, "status": "duplicate", "channels": []})
                    continue
                cfg = self._quiet_cfg(u)
                action = "pass"
                if cfg["enabled"] and self._is_quiet(cfg):
                    action = quiet.behavior_for(cfg, m.priority)
                if action == "skip":
                    results.append({"user": u, "status": "quiet_skipped", "channels": []})
                    continue
                if action == "defer":
                    if self._plan(channels, cats, u, cat) is None:
                        results.append({"user": u, "status": "off", "channels": []})
                        continue
                    self._enqueue(nid, u, cat, title, message, std, extra, raw)
                    results.append({"user": u, "status": "deferred", "channels": []})
                    continue
                if action == "silent":
                    m.silent = True
                results.append(await self._deliver_user(run, channels, cats, u, m))

        if std.get("expire") and run.refs:
            self._register_expiry(nid, std["expire"], run.refs)

        delivered = any(r["status"] == "sent" for r in results)
        self._journal(
            {
                "ts": time.time(),
                "id": nid,
                "category": cat,
                "title": _short(title or "", 80),
                "message": _short(message, 200),
                "priority": std["priority"],
                "test": test,
                "results": [
                    {
                        "user": r["user"],
                        "status": r["status"],
                        "channels": [
                            {k: v for k, v in c.items() if k != "shared"} for c in r["channels"]
                        ],
                    }
                    for r in results
                ],
            }
        )
        return {"id": nid, "delivered": delivered, "results": results}

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
                res = await self._deliver_user(run, channels, cats, user, m)
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


def notifier_of(hass: HomeAssistant) -> Notifier:
    return hass.data[DOMAIN]["notifier"]


__all__ = ["Notifier", "notifier_of", "entry_of", "label"]
