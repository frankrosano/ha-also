"""Constants for the ALSO integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_SCAN_INTERVAL, Platform

DOMAIN = "also"

# Config entry data keys (``expires_at`` is a unix timestamp in seconds).
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ID_TOKEN = "id_token"
CONF_EXPIRES_AT = "expires_at"

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
# fails as long as the push stream is connected or delivered data within three of those cadences
# plus a margin.
PUSH_CADENCE_SECONDS = 120
PUSH_FRESH_SECONDS = 3 * PUSH_CADENCE_SECONDS + 60

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

MANUFACTURER = "ALSO"

# ``unique_id`` of auto-discovered sensors is ``<vehicle_id>_shadow_<shadow key>``.
SHADOW_UNIQUE_ID_INFIX = "shadow_"

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

__all__ = [
    "CONF_ACCESS_TOKEN",
    "CONF_EMAIL",
    "CONF_EXPIRES_AT",
    "CONF_ID_TOKEN",
    "CONF_REFRESH_TOKEN",
    "CONF_SCAN_INTERVAL",
    "DEFAULT_SCAN_INTERVAL",
    "DOMAIN",
    "EXPLICIT_SHADOW_KEYS",
    "MANUFACTURER",
    "MAX_SCAN_INTERVAL",
    "MIN_SCAN_INTERVAL",
    "PLATFORMS",
    "PUSH_CADENCE_SECONDS",
    "PUSH_FRESH_SECONDS",
    "PUSH_WARN_AFTER_FAILURES",
    "SHADOW_UNIQUE_ID_INFIX",
    "UNKNOWN_PACKET_HISTORY",
    "VEHICLE_LIST_EVERY_N_UPDATES",
]
