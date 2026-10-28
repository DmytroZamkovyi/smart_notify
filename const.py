"""Константи Smart Notify."""

DOMAIN = "smart_notify"
COMMON = "common"
FALLBACK_SOURCE = "service:persistent_notification"
EXCLUDED_SERVICES = {"smart", "send_message", "persistent_notification", "notify"}

KIND_CHANNELS = "channels"
KIND_CATEGORIES = "categories"
KINDS = (KIND_CHANNELS, KIND_CATEGORIES)
ENTRY_TITLES = {
    KIND_CHANNELS: "Smart Notify · Канали",
    KIND_CATEGORIES: "Smart Notify · Категорії",
}