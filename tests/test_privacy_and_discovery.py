"""Diagnostics redaction by value, masking of unknown wire data, and stream-only discovery."""

from __future__ import annotations

import json
import pathlib
import struct

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyalso import (
    WIRE_LEN,
    WIRE_START_GROUP,
    EnergyStateEvent,
    RideInfoEvent,
    WireField,
    parse_fields,
)
from pyalso._wire import field_float32, field_string, field_varint
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import DOMAIN
from custom_components.also.diagnostics import async_get_config_entry_diagnostics
from custom_components.also.sensor import CHARGE_STATE_OPTIONS
from custom_components.also.unknown_packets import redact_payload_hex

from .common import BASE_TIME, VEHICLE_ID, make_entry, make_state, unknown_packet
from .conftest import PyalsoMocks, get_coordinator

# Secrets the server could return under keys the integration has never heard of. Built at runtime
# where they would look like real data.
FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 9 + "." + "c" * 6
FAKE_SN = "Q7" + "A" * 4 + "9" * 12
FAKE_OPAQUE = "A1" * 20


async def test_diagnostics_redacts_secrets_under_unfamiliar_keys(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    """Regression: redaction was by key name only, so an identifier under a key such as
    ``mystery`` was emitted unchanged (unknown shadow keys are kept on purpose)."""
    extra = {
        "mystery": "owner@example.org",
        "owner_blob": "VHC-AAAA-BBBB-EEEE",
        "foo_sTag": "11111111-1111-4111-8111-111111111111",
        "foo_sWho": "USR-AAAA-BBBB-DDDD",
        "foo_sJwt": FAKE_JWT,
        "foo_sOpaque": FAKE_OPAQUE,
        "foo_sSn": FAKE_SN,
        "foo_sBearer": "Bearer abcdefgh12345678",
        "foo_xBig": 123_456_789_012,
        "foo_xNeg": -98_765_432_101,
        "foo_rFloat": 3.456789,
        "foo_oNested": {"inner": "x@example.net", "deep": [{"n": -1_234_567_891_0}]},
        # Harmless values must survive: that is what the download is for.
        "foo_bFlag": True,
        "foo_rBattery": 91.25,
        "foo_sModel": "Test Model",
        "foo_nSmall": 42,
    }
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, extra
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)
    for secret in (
        "owner@example.org",
        "VHC-AAAA-BBBB-EEEE",
        "11111111-1111-4111-8111-111111111111",
        "USR-AAAA-BBBB-DDDD",
        FAKE_JWT,
        FAKE_OPAQUE,
        FAKE_SN,
        "abcdefgh12345678",
        str(123_456_789_012),
        str(98_765_432_101),
        "3.456789",
        "x@example.net",
        str(12_345_678_910),
    ):
        assert secret not in dump, secret
    shadow = diag["vehicles"][0]["shadow"]
    assert shadow["mystery"] == shadow["foo_sJwt"] == shadow["foo_xBig"] == REDACTED
    assert shadow["foo_oNested"]["inner"] == REDACTED
    assert shadow["foo_oNested"]["deep"][0]["n"] == REDACTED
    # ... while ordinary unknown values are kept.
    assert shadow["foo_bFlag"] is True
    assert shadow["foo_rBattery"] == 91.25
    assert shadow["foo_sModel"] == "Test Model"
    assert shadow["foo_nSmall"] == 42
    assert shadow["con_tUtc"] == BASE_TIME  # timestamps are not coordinates
    assert shadow["hu_xOdometer"] == 123456


def test_value_heuristics() -> None:
    from custom_components.also.privacy import is_coordinate_like_number, is_private_text

    assert is_private_text("someone@example.org")
    assert is_private_text("Bearer " + "x" * 12)
    assert not is_private_text("v2026.31.00-abcdef12")  # a firmware string
    assert not is_private_text("Test Model")
    assert is_coordinate_like_number(123_456_789_012)
    assert is_coordinate_like_number(-123_456_789)
    assert is_coordinate_like_number(3.45678)
    assert not is_coordinate_like_number(1_700_000_000)  # unix seconds
    assert not is_coordinate_like_number(87.5)
    assert not is_coordinate_like_number(91.25)
    assert not is_coordinate_like_number(123456)
    assert not is_coordinate_like_number(True)
    assert not is_coordinate_like_number(float("500.12345"))  # beyond +-180 degrees


# ------------------------------------------------------------------ unknown wire data
COORDINATE_FLOAT = 3.456789  # a made-up position carried as a 32-bit float


def test_fixed32_values_are_masked_in_unknown_packets() -> None:
    """Regression: fixed32 survived masking byte for byte, so a float coordinate in an unknown
    packet reached the downloadable diagnostics file."""
    payload = (
        field_varint(1, 7)
        + field_float32(5, COORDINATE_FLOAT)
        + field_bytes_group(
            9, field_float32(2, -COORDINATE_FLOAT) + field_string(3, "SECRETGROUPTEXT")
        )
    )
    redacted_hex = redact_payload_hex(payload)
    assert redacted_hex is not None
    for leak in (
        struct.pack("<f", COORDINATE_FLOAT).hex(),
        struct.pack("<f", -COORDINATE_FLOAT).hex(),
        b"SECRETGROUPTEXT".hex(),
    ):
        assert leak not in redacted_hex, leak
    fields = {f.number: f for f in parse_fields(bytes.fromhex(redacted_hex))}
    assert fields[1].value == 7  # small values are still readable
    assert fields[5].value == 0 and fields[5].wire_type == 5  # kept as a field, value masked
    assert fields[9].wire_type == WIRE_START_GROUP  # a group is redacted, not rejected
    inner = {f.number: f for f in parse_fields(bytes(fields[9].value))}
    assert inner[2].value == 0
    assert inner[3].value == b"x" * len("SECRETGROUPTEXT")


def field_bytes_group(number: int, body: bytes) -> bytes:
    return WireField(number, WIRE_START_GROUP, body).encode()


async def test_diagnostics_never_contain_a_float_coordinate_from_unknown_packets(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    payload = field_float32(5, COORDINATE_FLOAT) + field_bytes_group(
        9, field_float32(1, COORDINATE_FLOAT)
    )
    mock_pyalso.streams.latest().emit(unknown_packet(payload))
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    dump = json.dumps(diag, default=str)
    assert struct.pack("<f", COORDINATE_FLOAT).hex() not in dump
    assert diag["unknown_packet_count"] == 1
    assert "payload_hex_redacted" in diag["unknown_packets"][0]


# ----------------------------------------------------------------------- discovery
def _energy(power_state: int = 3) -> EnergyStateEvent:
    return EnergyStateEvent(
        feature=0x03,
        command=0x0102,
        status=0xFF,
        timestamp=BASE_TIME,
        payload=b"",
        power_state_raw=power_state,
        range_predicted_raw=20.5,
        power_consumed_average_raw=150.0,
    )


def _ride() -> RideInfoEvent:
    return RideInfoEvent(
        feature=0x03,
        command=0x0101,
        status=0xFF,
        timestamp=BASE_TIME,
        payload=b"",
        drive_mode_raw=1,
        ride_mode_raw=2,
        assist_level=3,
        gear=4,
    )


async def test_stream_only_values_become_disabled_diagnostic_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    """Regression: discovery only looked at the REST shadow, so push-only values (speed, range,
    assist level, gear, ...) could never become entities."""
    registry = er.async_get(hass)
    stream = mock_pyalso.streams.latest()
    stream.emit(_energy())
    stream.emit(_ride())
    await hass.async_block_till_done()

    def uid(key: str) -> str:
        return f"{VEHICLE_ID}_push_{key}"

    for key in (
        "range_predicted_raw",
        "power_consumed_average_raw",
        "assist_level",
        "gear",
        "drive_mode_raw",
    ):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid(key))
        assert entity_id, key
        reg = registry.async_get(entity_id)
        assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert reg.entity_category is EntityCategory.DIAGNOSTIC
        assert "(stream)" in (reg.original_name or "")
    # Never discovered: values that already have an entity, and strings.
    stream.emit(
        __import__("tests.common", fromlist=["battery_event"]).battery_event(55, at=BASE_TIME + 100)
    )
    await hass.async_block_till_done()
    for key in (
        "battery_level_percent",
        "charge_limit_percent",
        "minutes_till_full",
        "battery_health",
        "battery_charging_state_raw",
        "battery_serial",
        "firmware_version",
    ):
        assert registry.async_get_entity_id("sensor", DOMAIN, uid(key)) is None, key
    # The stream's power/ride enums are different from the shadow's: separate entities.
    shadow_power = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_power_state_raw")
    stream_power = registry.async_get_entity_id("sensor", DOMAIN, uid("power_state_raw"))
    assert shadow_power and stream_power and shadow_power != stream_power
    assert hass.states.get(shadow_power).state == "1"  # the REST shadow value, untouched


async def test_enabled_stream_sensor_shows_the_raw_value_and_ids_do_not_collide(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    registry = er.async_get(hass)
    mock_pyalso.streams.latest().emit(_energy(power_state=7))
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_push_power_state_raw")
    assert entity_id
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    mock_pyalso.streams.latest().emit(_energy(power_state=7))
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None and state.state == "7"
    assert "unit_of_measurement" not in state.attributes and "device_class" not in state.attributes
    unique_ids = [e.unique_id for e in registry.entities.values()]
    assert len(unique_ids) == len(set(unique_ids))
    assert get_coordinator(init_integration).data[VEHICLE_ID].push["power_state_raw"] == 7


# ------------------------------------------------------------------ small entity fixes
async def test_charge_limit_has_a_state_class(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{VEHICLE_ID}_charge_limit"
    )
    assert entity_id
    assert hass.states.get(entity_id).attributes["state_class"] == "measurement"


def test_every_charge_state_option_is_translated_and_both_string_files_agree() -> None:
    base = pathlib.Path(__file__).parents[1] / "custom_components/also"
    strings = json.loads((base / "strings.json").read_text())
    english = json.loads((base / "translations/en.json").read_text())
    assert strings == english
    labels = strings["entity"]["sensor"]["charge_state"]["state"]
    assert set(CHARGE_STATE_OPTIONS) <= set(labels)
    assert labels["unknown_2"] == "Unknown (2)"  # neutral: the meaning was never observed
    assert "token_store_failed" in strings["issues"]
    assert WIRE_LEN == 2  # (the public re-export used by the diagnostics code)
