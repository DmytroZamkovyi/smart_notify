# Smart Notify

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DmytroZamkovyi&repository=smart_notify&category=integration)

A Home Assistant custom integration that gives scripts and automations **one
notification service** and lets every person decide, per category, **where**
they want to receive it.

```yaml
action: notify.smart
data:
  user: person.alice        # optional; list allowed
  category: security        # a category you defined
  title: Motion detected
  message: Front gate camera
  data:                     # optional, passed to the channel (see below)
    image: /local/snapshot.jpg
```

No helpers, no YAML configuration: everything is set up in the UI.

## Features

- One service, `notify.smart`, with a standard notify-style interface
  (`message`, `title`, `data`) plus `user` and `category`.
- **Channels**: phones (Companion app), Telegram bots, browsers (HTML5 push)
  or any other `notify` service/entity. Every channel has an owner (a person
  or "Common").
- **Categories**: created by the admin (security, climate, appliances, ...).
  Shown as a dropdown in the action form.
- **Routes**: per person and per category:
  - disabled;
  - send to **several channels at once**;
  - **fallback** channels, tried in order only if every main channel failed
    (for example Telegram, then phone).
- "HA panel" (persistent notification) is a regular selectable channel.
- Every person edits their own routes on a page at `/smart-notify`
  (no sidebar entry). Admins can edit everybody's routes.
- Native UI only: config entries with subentries, nothing to maintain in YAML.

## Requirements

- Home Assistant with config subentries support (2025.3 or newer).
  Tested on 2026.10.
- For browser notifications: the HTML5 Push Notifications integration and an
  HTTPS external address.

## Installation

HACS: click the button at the top of this page, or add the repository by hand:

1. HACS, Integrations, three dots, Custom repositories.
2. Add this repository, category Integration.
3. Install Smart Notify and restart Home Assistant.

Manual:

1. Copy the contents of this repository into a folder named `smart_notify`
   inside your `config/custom_components/` directory, so that
   `config/custom_components/smart_notify/manifest.json` exists.
2. Restart Home Assistant.

Then: Settings, Devices & services, Add integration, **Smart Notify**. Two
blocks appear:

- **Smart Notify · Channels**: add channels here.
- **Smart Notify · Categories**: add categories and configure routes here.

## Setup

1. **Channels** block, "Add channel": choose what to connect, give it a name
   and an owner. Add one channel per phone, Telegram bot or browser.
   - Owner = a person: used only for that person's routes.
   - Owner = "Common": used when `notify.smart` is called without `user`.
2. **Categories** block, "Add category": an ID (latin, digits, `_`) used in
   scripts, and a display name.
3. Routes, either way:
   - each person opens `/smart-notify` (log in as that person; the HA user
     must be linked to a `person`);
   - or the admin uses the gear icon of the Categories block.

## Routing rules

- A category setting can be **As in general route**, **Off** or **Send**.
  Each person also has a general route that categories inherit from.
- **Send**: all channels in "Send to" are used at once. Channels in
  "If not delivered" are tried one by one, in the order they were selected,
  only if none of the main channels succeeded, and stop at the first success.
- Nothing configured: the persistent notification in the HA panel is used,
  so a message is never silently lost.
- If every selected channel fails, the error is logged and the message goes to
  the HA panel as a last resort.
- Without `user`: all "Common" channels; if there are none, everybody.
- `user: all` sends to every person.
- The same channel is never used twice in one call.
- Unknown category: logged as a warning, general routes apply.

## Channel types and `data`

`data` is passed to the channel as follows.

| Channel | What happens to `data` |
|---|---|
| Phone (`notify.mobile_app_*`) | Passed as is: `image`, `tag`, `group`, `actions`, `channel`, `ttl`, `priority`, ... |
| Telegram (Telegram bot notify entity) | Mapped to `telegram_bot.send_message` / `send_photo`: `photo` (or `image`), `caption`, `parse_mode`, `keyboard`, `inline_keyboard`, `disable_notification`, `disable_web_page_preview`, `message_tag`, `reply_to_message_id`, `message_thread_id` |
| Browser (HTML5 notify entity) | Mapped to `html5.send_message`: `icon`, `badge`, `image`, `tag`, `actions`, `renotify`, `silent`, `require_interaction`, `vibrate`, `ttl`, `urgency`, ... |
| Other `notify` services | Passed as is |
| Other `notify` entities | `data` is dropped, only `title` and `message` |

Keys for different channel types can be combined in one `data`; each channel
uses its own and ignores the rest.

```yaml
action: notify.smart
data:
  user:
    - person.alice
    - person.bob
  category: security
  title: Motion in the yard
  message: "<b>Front gate</b>"
  data:
    image: https://example.invalid/snapshot.jpg   # phone
    photo: https://example.invalid/snapshot.jpg   # Telegram
    tag: gate-motion
    parse_mode: html
    inline_keyboard:
      - "Cameras:/cameras, Acknowledge:/ack"
```

## Browser (HTML5 push)

1. Add the HTML5 Push Notifications integration.
2. Open Home Assistant over its external HTTPS address in the browser you want
   to subscribe.
3. User profile, "This browser", enable notifications, name the device, allow.
4. A `notify` entity appears. Add it as a channel in Smart Notify and set the
   person who uses that browser as the owner.

Each person subscribes their own browser and gets their own channel.

## Limitations

- Telegram and browser notifications need the corresponding integrations to be
  set up first.
- Removing a channel that is used in a route makes the route skip it (a
  warning is logged).
- The `/smart-notify` page needs the HA user to be linked to a `person`.

## License

MIT