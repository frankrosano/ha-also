"""Constants for the ALSO integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_SCAN_INTERVAL, Platform

DOMAIN = "also"

# Config entry data keys (``expires_at`` is a unix timestamp in seconds).
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ID_TOKEN = "id_token"
CONF_EXPIRES_AT = "expires_at"
# Both immutable account identifiers, stored at setup so a reauth can be matched against either
# (the API may expose a uuid, a reference id or both, and which ones are present can change).
CONF_USER_UUID = "user_uuid"
CONF_USER_REFERENCE_ID = "user_reference_id"
# REST is the slower companion of the live push stream: it only fills in shadow-only fields.
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 3600

# The vehicle list changes rarely, so it is only re-fetched every Nth update (or when empty).
VEHICLE_LIST_EVERY_N_UPDATES = 10

# After this many failed connection attempts in a row the integration logs ONE warning (per outage)
# and keeps working on polling alone; the stream itself keeps retrying with backoff.
PUSH_WARN_AFTER_FAILURES = 3

# How many distinct unknown stream packets diagnostics keep (rolling window; repeats are counted).
UNKNOWN_PACKET_HISTORY = 20

# The cloud pushes the battery state about every 120 s. Entities stay available while the REST poll
# fails as long as the push stream DELIVERED DATA within three of those cadences plus a margin. A
# stream that is merely connected (keep-alive replies only) does not count: transport health and
# data freshness are different things.
PUSH_CADENCE_SECONDS = 120
PUSH_FRESH_SECONDS = 3 * PUSH_CADENCE_SECONDS + 60
# The per-bike expiry timer fires this long after PUSH_FRESH_SECONDS so the check is strictly past.
PUSH_EXPIRY_GRACE_SECONDS = 1

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

MANUFACTURER = "ALSO"

# ``unique_id`` of auto-discovered sensors is ``<vehicle_id>_shadow_<shadow key>`` for REST shadow
# keys and ``<vehicle_id>_push_<push key>`` for values only the live stream delivers (a separate
# namespace, so a key that exists in both places can never collide).
SHADOW_UNIQUE_ID_INFIX = "shadow_"
PUSH_UNIQUE_ID_INFIX = "push_"
# Shadow keys that already have a dedicated entity (or are position data). Everything else that is
# numeric/boolean is exposed through disabled-by-default auto-discovered diagnostic sensors.
SHADOW_KEY_SOC_DISPLAY = "bms_rSOCDisp"
SHADOW_KEY_SOC_REAL = "bms_rSOCReal"
SHADOW_KEY_SOC_CHARGE_LIMIT = "bms_rSOCChargeSetFb"
SHADOW_KEY_ODOMETER = "hu_xOdometer"
SHADOW_KEY_CHARGE_STATE = "con_NChargeState"
SHADOW_KEY_POWER_STATE = "con_NBikePowerState"
SHADOW_KEY_RIDE_MODE = "hu_NRideModeREQ"
SHADOW_KEY_FAULT = "pcm_NFault"
SHADOW_KEY_SAT_COUNT = "con_nSatCount"
SHADOW_KEY_TIME_UTC = "con_tUtc"
SHADOW_KEY_LATITUDE = "con_aLatitude"
SHADOW_KEY_LONGITUDE = "con_aLongitude"
SHADOW_KEY_ALTITUDE = "con_zAltitude"

EXPLICIT_SHADOW_KEYS: frozenset[str] = frozenset(
    {
        SHADOW_KEY_SOC_DISPLAY,
        SHADOW_KEY_SOC_REAL,
        SHADOW_KEY_SOC_CHARGE_LIMIT,
        SHADOW_KEY_ODOMETER,
        SHADOW_KEY_CHARGE_STATE,
        SHADOW_KEY_POWER_STATE,
        SHADOW_KEY_RIDE_MODE,
        SHADOW_KEY_FAULT,
        SHADOW_KEY_SAT_COUNT,
        SHADOW_KEY_TIME_UTC,
        SHADOW_KEY_LATITUDE,
        SHADOW_KEY_LONGITUDE,
        SHADOW_KEY_ALTITUDE,
    }
)

# Stream-only (``state.push``) keys that already have a dedicated entity, are the stream's copy of a
# value an explicit entity shows (state of charge and charge limit: ``battery_level`` and
# ``charge_limit`` pick the newer of REST and stream), or are not numbers. Everything else numeric
# is exposed through disabled-by-default diagnostic sensors. Position-like keys are excluded by
# ``privacy.is_position_key``.
EXPLICIT_PUSH_KEYS: frozenset[str] = frozenset(
    {
        "battery_level_percent",
        "charge_limit_percent",
        "minutes_till_full",
        "battery_health",
        "battery_charging_state_raw",
        "firmware_version",
        "model_name",
    }
)

__all__ = [
    "CONF_ACCESS_TOKEN",
    "CONF_EMAIL",
    "CONF_EXPIRES_AT",
    "CONF_ID_TOKEN",
    "CONF_REFRESH_TOKEN",
    "CONF_SCAN_INTERVAL",
    "CONF_USER_REFERENCE_ID",
    "CONF_USER_UUID",
    "DEFAULT_SCAN_INTERVAL",
    "DOMAIN",
    "EXPLICIT_PUSH_KEYS",
    "EXPLICIT_SHADOW_KEYS",
    "MANUFACTURER",
    "MAX_SCAN_INTERVAL",
    "MIN_SCAN_INTERVAL",
    "PLATFORMS",
    "PUSH_CADENCE_SECONDS",
    "PUSH_EXPIRY_GRACE_SECONDS",
    "PUSH_FRESH_SECONDS",
    "PUSH_UNIQUE_ID_INFIX",
    "PUSH_WARN_AFTER_FAILURES",
    "SHADOW_UNIQUE_ID_INFIX",
    "UNKNOWN_PACKET_HISTORY",
    "VEHICLE_LIST_EVERY_N_UPDATES",
]
