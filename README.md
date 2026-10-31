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
- **Categories and subcategories**: created by the admin (security, climate,
  power with "schedule changes" and "emergency outages", ...). Shown as a
  dropdown in the action form. Optional title prefix, admin default (on/off for
  everybody) and a "service" flag for developer-only categories.
- **Routes**: per person and per category:
  - disabled;
  - send to **several channels at once**;
  - **fallback** channels, tried in order only if every main channel failed
    (for example Telegram, then phone).
- "HA panel" (persistent notification) is a regular selectable channel.
- Every person edits their own routes on a page at `/smart-notify`
  (no sidebar entry). Admins can edit everybody's routes.
- **Standard `data`** (`image`, `url`, `actions`, `tag`, `priority`, `format`,
  `sticky`, `clear`, `expire`, `progress`) that works the same for phone,
  Telegram and browser; Telegram progress bars edited in place, long-text
  splitting, expiry, camera snapshots.
- **Quiet hours** per person (schedule and/or entity condition) with per-priority
  behavior, deferred delivery and summaries; duplicate window; journal; test button.
- The action returns a delivery result; recipients can be filtered by presence.
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
   scripts, and a display name. Optional: parent category (makes it a
   subcategory), title prefix, default for everyone, service flag.
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
- `category` is required. An unknown category, or a category that has
  subcategories, makes the action fail with an error (visible in the
  automation trace).

## Subcategories, defaults and prefix

A category with subcategories is only a **group**: you cannot send to it, it
only holds shared settings for its subcategories. In scripts you use the
subcategory ID, for example `category: emergency_outage` instead of `power`.
Only one nesting level is supported, and IDs are unique across categories and
subcategories.

Which setting applies to a person, first match wins:

1. The person's own setting of the subcategory, then of its category (the first
   one that is not "As in ..."). **Off** means silence, **Send** uses the
   person's channels. A subcategory set to **Send** wins even if its category
   is **Off**.
2. The admin default of the subcategory, then of its category. **Off for
   everyone** means silence until a person explicitly sets **Send**
   (use it for a developer-only `dev` category, together with the "service"
   flag that hides it from users on the settings page).
3. The person's general route.
4. Nothing configured, or every channel failed: persistent notification in the
   HA panel. A deliberate **Off** never creates one.

**Prefix**: the prefix of the category and of the subcategory are joined in
brackets, for example `[Power][Emergency]`, and put in front of the title. With
no `title`, the prefix itself becomes the title and `message` stays as is.
Templates in `title`, `message` and `data` work as usual (Home Assistant renders
them before calling the action); the prefix itself is plain text.

If you delete a category that has subcategories, they become regular top-level
categories and a notification tells you so (Home Assistant cannot block that
delete).

## Upgrading to 5.0

`category` is now mandatory and unknown categories are an error (they used to
be a log warning). Scripts that call `notify.smart` without a category, or with a
category that does not exist, must be fixed. Existing categories and routes keep
working with no migration.

## Standard `data`

One `data` works for every channel; each channel maps it to its own format. Keys
that are not in this list are channel-specific and are passed through (a phone
gets its own, Telegram its own).

| Key | Meaning |
|---|---|
| `image` | One picture for all channels: a URL, `/local/file.jpg` (`config/www`), `/media/...` or a file path, or `{camera: camera.gate}` for a camera snapshot |
| `url` | Opens on tap (phone) or as a "Open" button (Telegram, browser) |
| `actions` | Buttons: `{title, id}` or a link button `{title, url}` |
| `tag` | Replaces the previous notification with the same tag |
| `priority` | `low`, `normal`, `high`, `critical` |
| `format` | `plain` (default), `html`, `markdown` (Telegram) |
| `sticky` | The notification is not dismissed by a tap |
| `clear` | With `tag`: remove that notification from the phone |
| `expire` | Lifetime: seconds or `30m`, `2h`, `1d` |
| `progress` | `0..100` or `{value, max}` |

| Channel | Mapping |
|---|---|
| Phone | `image`, `tag`, `sticky`, `progress`, `actions` (link buttons as `URI`), `url` as `clickAction`; the category icon becomes `notification_icon`, the category name `channel` and `group`; `priority` becomes Android importance / iOS interruption level |
| Telegram | Text or photo; the title is bold with `html`/`markdown`; `actions` and `url` become inline buttons; `priority: low` and quiet-hours "silent" send without sound; legacy keys (`photo`, `caption`, `parse_mode`, `inline_keyboard`, `keyboard`, `disable_notification`, ...) still work |
| Browser (HTML5) | `image`, `tag`, `actions`, `url`, `sticky`/`high` as require-interaction, `expire` as TTL |
| Other `notify` services | `data` is passed as is |
| Other `notify` entities | `data` is dropped, only `title` and `message` |

```yaml
action: notify.smart
data:
  user: person.alice
  category: security
  title: Motion in the yard
  message: Front gate
  data:
    image: {camera: camera.gate}
    priority: high
    tag: gate-motion
    expire: 30m
    actions:
      - {title: Cameras, url: "https://example.invalid/cameras"}
```

### Telegram details

- A text over 4096 characters is split into several messages; with a picture the
  caption is limited to 1024, the rest follows as separate messages.
- **Progress**: the first call with `progress` sends a message with a bar
  (`▓▓▓░░░░░░░ 30%`); next calls with the same `tag` edit it in place. At 100% the
  message is edited to a full bar (it is not deleted) and the final text is sent
  as a new message. Without `tag` the category is the key.
- **Expire**: after `expire` the phone notification is cleared and the Telegram
  message is edited to start with "НЕАКТУАЛЬНО" (it is not deleted).
- A camera snapshot for Telegram is saved to a temporary file in the HA media
  folder and removed right after sending.

## Recipients and result

Extra fields: `presence` (`any`, `home`, `away`, by `person` state) and
`exclude` (persons to skip). The action returns a response with the delivery
result per person and channel (`sent`, `failed`, `off`, `filtered`, `duplicate`,
`quiet_skipped`, `deferred`):

```yaml
action: notify.smart
data: {category: security, message: Test}
response_variable: res
```

## Quiet hours

Every person sets their own quiet hours on the `/smart-notify` page: several
intervals per day, chosen weekdays, from-to (also over midnight), and/or a
dynamic condition on any entity (for example a phone sleep or focus sensor).
What happens during quiet hours depends on the priority (defaults):

| Priority | Default | Options |
|---|---|---|
| `low` | skip | skip, silent, defer |
| `normal` | defer | skip, silent, defer |
| `high` | silent | skip, silent, defer |
| `critical` | always delivered | |

Deferred notifications are kept (also across restarts) and delivered when quiet
hours end (checked every minute): up to 3 per category one by one with their
original time, more than that as one summary per category. A deferred
notification that outlived its `expire` is dropped; without `expire` the default
lifetime is 24 hours (Options, "General settings").

## Duplicates, journal, test

- **Duplicates**: a category has "Do not repeat an identical notification
  within (min)". Same title and text to the same person is skipped
  (`duplicate`). Messages with `progress` or `clear` are never treated as duplicates.
- **Journal**: the last 200 notifications and their results; visible to admins
  on `/smart-notify`.
- **Test**: the "Send test" button on `/smart-notify` sends through the person's
  real routes and quiet hours for a chosen category and priority.

## Upgrading to 5.1

No breaking changes. Existing `data` keys keep working. Quiet hours and
duplicate windows are off until configured.

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