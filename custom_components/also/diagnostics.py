"""Diagnostics for the ALSO integration.

This is the ONLY place where full raw payloads leave the integration, and they are redacted. Unknown
shadow keys are deliberately kept: surfacing new data is the point of the download.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant
from pyalso.fields import SENSITIVE_PUSH_KEYS

from .coordinator import AlsoConfigEntry
from .privacy import has_position_substring, has_position_word, is_secret_key

TO_REDACT = {
    "email",
    "handle",
    "access_token",
    "refresh_token",
    "id_token",
    "session",
    "serial_number",
    "con_aLatitude",
    "con_aLongitude",
    "con_zAltitude",
    "latitude",
    "longitude",
    "altitude",
    "user_id",
    "user_reference_id",
    "user_uuid",
    "external_auth_id",
    "given_name",
    "family_name",
    "vehicle_reference_id",
    "name",
} | set(SENSITIVE_PUSH_KEYS)


def _is_private(name: str, value: Any) -> bool:
    if has_position_substring(name) or is_secret_key(name):
        return True
    return has_position_word(name) and not isinstance(value, Mapping | list)


def _redact_positions(data: Any) -> Any:
    """Second pass by key NAME, so a position or serial field the server adds later cannot leak.

    * keys spelling out a coordinate (``...latitude``, ``...longitude``, ``...altitude``, ``lng``)
      and keys that look like secrets or serial numbers are redacted whatever they hold;
    * keys with a short position word (``lat``, ``lon``, ``gps``, ``pos``, ...) are redacted when
      the value is a scalar, so containers such as ``gps_info`` are still inspected key by key.
    """
    if isinstance(data, Mapping):
        result: dict[str, Any] = {}
        for key, value in data.items():
            name = str(key)
            if value is None:
                result[key] = None
            elif _is_private(name, value):
                result[key] = REDACTED
            else:
                result[key] = _redact_positions(value)
        return result
    if isinstance(data, list):
        return [_redact_positions(item) for item in data]
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AlsoConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    vehicles = [
        async_redact_data(_redact_positions(state.raw), TO_REDACT)
        for state in coordinator.data.values()
    ]
    push = []
    for vehicle_id, state in coordinator.data.items():
        status = coordinator.push_status(vehicle_id)
        push.append(
            {
                # The stream's and the REST shadow's charge states are DIFFERENT enums; both raw
                # numbers are shown side by side so a mismatch is visible.
                "charging_state_raw": state.battery_charging_state_raw,
                "shadow_charge_state_raw": state.charge_state_raw,
                "connected": status.connected,
                "last_status": status.reason,
                "last_event": status.last_event.isoformat() if status.last_event else None,
                "failed_attempts": status.failures,
                "polling_only": status.unavailable,
            }
        )
    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "last_update_success": coordinator.last_update_success,
        "push_enabled": coordinator.push_enabled,
        # Same order as ``vehicles``; the bike ids are left out on purpose.
        "push": push,
        # Packets and fields pyalso could not decode (newest last), values masked, see
        # custom_components/also/unknown_packets.py.
        "unknown_packet_count": coordinator.unknown_packet_count,
        "unknown_packets": list(coordinator.unknown_packets),
        "vehicles": vehicles,
    }
