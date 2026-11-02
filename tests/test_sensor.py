"""Sensor, binary sensor and device tracker tests."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyalso import AlsoConnectionError, Connectivity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import DOMAIN
from custom_components.also.sensor import (
    BMS_STATE_OPTIONS,
    KNOB_POSITION_OPTIONS,
    POWER_STATE_OPTIONS,
    RIDE_MODE_OPTIONS,
)

from .common import (
    BIKE_NAME,
    LAT_NANO,
    LON_NANO,
    SERIAL,
    VEHICLE_ID,
    make_state,
)
from .conftest import PyalsoMocks, get_coordinator


def _entity(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{VEHICLE_ID}_{key}")
    assert entity_id, f"no {platform} entity for {key}"
    return entity_id


async def test_sensor_states(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    expected = {
        "battery_level": "87.5",
        "charge_limit": "90.0",
        "charge_state": "fully_charged",  # REST con_NChargeState 3, mapped (no stream yet)
        "ride_mode": "sport_manual",  # REST hu_NRideModeREQ 2 (an app name)
        "power_state": "unknown_1",  # REST con_NBikePowerState 1: never observed
        "knob_position": "unknown_4",  # REST hu_NModeSelectorKnob 4: outside 0-3
        "bms_state": "standby",  # REST bms_NState 2
        "fault_count": "1",
        "gps_satellites": "9",
        "last_location_update": "2024-05-01T12:00:00+00:00",
        "speed": "0.0",  # REST pcm_vVehicleFilt 0 mph
    }
    for key, value in expected.items():
        state = hass.states.get(_entity(hass, "sensor", key))
        assert state is not None and state.state == value, key
    # The REST-only charge-state sensor is gone: its number is an attribute of charge_state.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_charge_state_raw") is None
    charge = hass.states.get(_entity(hass, "sensor", "charge_state")).attributes
    assert (charge["source"], charge["rest_raw"], charge["stream_raw"]) == ("rest", 3, None)
    for key in ("battery_level", "charge_limit", "odometer", "gps_satellites", "speed"):
        attrs = hass.states.get(_entity(hass, "sensor", key)).attributes
        assert attrs["source"] == "rest" and attrs["stream_raw"] is None, key
    assert hass.states.get(_entity(hass, "sensor", "battery_level")).attributes["rest_raw"] == 87.5

    faults = hass.states.get(_entity(hass, "sensor", "fault_count")).attributes
    assert (faults["fault_level_raw"], faults["fault_codes"]) == (2, None)
    power = hass.states.get(_entity(hass, "sensor", "power_state")).attributes
    assert (power["rest_raw"], power["candidate_name"]) == (1, "OFF")
    ride = hass.states.get(_entity(hass, "sensor", "ride_mode")).attributes
    assert ride["rest_raw"] == 2 and "candidate_name" not in ride
    battery = hass.states.get(_entity(hass, "sensor", "battery_level"))
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.attributes["device_class"] == "battery"


async def test_entity_categories_and_enum_options(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for key, options in (
        ("power_state", POWER_STATE_OPTIONS),
        ("knob_position", KNOB_POSITION_OPTIONS),
        ("bms_state", BMS_STATE_OPTIONS),
    ):
        entry = registry.async_get(_entity(hass, "sensor", key))
        assert entry.entity_category is EntityCategory.DIAGNOSTIC, key
        attrs = hass.states.get(entry.entity_id).attributes
        assert attrs["device_class"] == "enum", key
        assert attrs["options"][: len(options)] == options, key
    ride = registry.async_get(_entity(hass, "sensor", "ride_mode"))
    assert ride.entity_category is None
    attrs = hass.states.get(ride.entity_id).attributes
    assert attrs["device_class"] == "enum" and attrs["options"] == RIDE_MODE_OPTIONS
    speed = registry.async_get(_entity(hass, "sensor", "speed"))
    assert speed.entity_category is None
    attrs = hass.states.get(speed.entity_id).attributes
    assert (attrs["device_class"], attrs["state_class"]) == ("speed", "measurement")
    # A plain diagnostic integer stays one.
    sats = registry.async_get(_entity(hass, "sensor", "gps_satellites"))
    assert sats.entity_category is EntityCategory.DIAGNOSTIC
    attrs = hass.states.get(sats.entity_id).attributes
    assert "options" not in attrs and "device_class" not in attrs
    assert "unit_of_measurement" not in attrs


async def _refresh_with(
    hass: HomeAssistant, entry: MockConfigEntry, mocks: PyalsoMocks, shadow: dict[str, Any]
) -> None:
    # Through the fake client's REST payloads, so the values carry a fresh report time.
    mocks.rest_state = lambda vid: make_state(vid, shadow)
    await get_coordinator(entry).async_refresh()
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "unknown_0"),  # none selected (asleep or parked)
        (1, "sport"),
        (2, "sport_manual"),
        (3, "trail_manual"),
        (4, "all_purpose"),
        (5, "conserve"),
        (6, "trail_cadence"),
        (7, "custom"),
        (8, "all_purpose_manual"),
        (12, "unknown_12"),
    ],
)
async def test_ride_mode_mapping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    raw: int,
    expected: str,
) -> None:
    await _refresh_with(hass, init_integration, mock_pyalso, {"hu_NRideModeREQ": raw})
    state = hass.states.get(_entity(hass, "sensor", "ride_mode"))
    assert state.state == expected
    assert state.attributes["rest_raw"] == raw
    assert "candidate_name" not in state.attributes
    options = state.attributes["options"]
    assert options[: len(RIDE_MODE_OPTIONS)] == RIDE_MODE_OPTIONS
    assert expected in options
    assert len(options) == len(RIDE_MODE_OPTIONS) + expected.startswith("unknown_")


@pytest.mark.parametrize(
    ("raw", "expected", "candidate"),
    [
        (2, "awake_battery_off", "TWELVEVOLTHIGH"),
        (3, "asleep", "TWELVEVOLTLOW"),
        (4, "battery_on", "VBATTENABLED"),
        (0, "unknown_0", "UNSPECIFIED"),
        (5, "unknown_5", "RESERVE"),
        (9, "unknown_9", None),
    ],
)
async def test_power_state_mapping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    raw: int,
    expected: str,
    candidate: str | None,
) -> None:
    await _refresh_with(hass, init_integration, mock_pyalso, {"con_NBikePowerState": raw})
    state = hass.states.get(_entity(hass, "sensor", "power_state"))
    assert state.state == expected
    assert (state.attributes["rest_raw"], state.attributes["candidate_name"]) == (raw, candidate)
    options = state.attributes["options"]
    assert options[: len(POWER_STATE_OPTIONS)] == POWER_STATE_OPTIONS and expected in options


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, "display_off"), (1, "locked"), (2, "ready"), (3, "ride"), (7, "unknown_7")],
)
async def test_knob_position_mapping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    raw: int,
    expected: str,
) -> None:
    await _refresh_with(hass, init_integration, mock_pyalso, {"hu_NModeSelectorKnob": raw})
    state = hass.states.get(_entity(hass, "sensor", "knob_position"))
    assert state.state == expected and state.attributes["rest_raw"] == raw
    options = state.attributes["options"]
    assert options[: len(KNOB_POSITION_OPTIONS)] == KNOB_POSITION_OPTIONS and expected in options


@pytest.mark.parametrize(("raw", "expected"), [(2, "standby"), (4, "active"), (5, "unknown_5")])
async def test_bms_state_mapping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    raw: int,
    expected: str,
) -> None:
    await _refresh_with(hass, init_integration, mock_pyalso, {"bms_NState": raw})
    state = hass.states.get(_entity(hass, "sensor", "bms_state"))
    assert state.state == expected and state.attributes["rest_raw"] == raw
    options = state.attributes["options"]
    assert options[: len(BMS_STATE_OPTIONS)] == BMS_STATE_OPTIONS and expected in options


async def test_unknown_option_leaves_the_options_when_the_state_moves_on(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    await _refresh_with(hass, init_integration, mock_pyalso, {"bms_NState": 5})
    entity_id = _entity(hass, "sensor", "bms_state")
    assert hass.states.get(entity_id).attributes["options"] == [*BMS_STATE_OPTIONS, "unknown_5"]
    await _refresh_with(hass, init_integration, mock_pyalso, {"bms_NState": 4})
    assert hass.states.get(entity_id).attributes["options"] == BMS_STATE_OPTIONS


async def test_fault_codes_attribute(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    fault = {"NFaultCount": 1, "NFaultLevel": 7, "NFaultRolling": {"1200": 1_700_000_000}}
    await _refresh_with(hass, init_integration, mock_pyalso, {"pcm_NFault": fault})
    attrs = hass.states.get(_entity(hass, "sensor", "fault_count")).attributes
    assert attrs["fault_codes"] == {"1200": "2023-11-14T22:13:20+00:00"}
    assert attrs["fault_level_raw"] == 7
    cleared = {"NFaultCount": 0, "NFaultLevel": 1, "NFaultRolling": {}}
    await _refresh_with(hass, init_integration, mock_pyalso, {"pcm_NFault": cleared})
    attrs = hass.states.get(_entity(hass, "sensor", "fault_count")).attributes
    assert attrs["fault_codes"] == {}


async def test_real_battery_level_enabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entry = er.async_get(hass).async_get(_entity(hass, "sensor", "battery_level_real"))
    assert entry.disabled_by is None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    state = hass.states.get(entry.entity_id)
    assert state is not None and state.state == "91.25"


async def test_every_entity_is_enabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    assert entries
    assert [e.entity_id for e in entries if e.disabled_by is not None] == []


async def test_device_registry(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, VEHICLE_ID)})
    assert device is not None
    assert device.manufacturer == "ALSO"
    assert device.model == "TEST-MODEL"
    assert device.serial_number == SERIAL
    assert device.name == BIKE_NAME


async def test_entities_unavailable_on_update_failure(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.client.get_vehicle_state.side_effect = AlsoConnectionError()
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    for platform, key in (("sensor", "battery_level"), ("binary_sensor", "connected")):
        assert hass.states.get(_entity(hass, platform, key)).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity(hass, "device_tracker", "location")).state == STATE_UNAVAILABLE


async def test_binary_sensor_connected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    entity_id = _entity(hass, "binary_sensor", "connected")
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["device_class"] == "connectivity"
    assert state.attributes["disconnect_reason"] == "TEST_REASON"
    assert state.attributes["changed_at"] == "2024-05-01T11:00:00+00:00"

    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, connected=False
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, connected=None
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    def asleep(vid: str, vehicle: object = None):
        state = make_state(vid)
        connectivity = Connectivity.from_dict(
            {"connected": False, "disconnect_reason": "MQTT_KEEP_ALIVE_TIMEOUT"}
        )
        return type(state)(vid, state.vehicle, state.shadow, state.location, connectivity)

    mock_pyalso.client.get_vehicle_state.side_effect = asleep
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["disconnect_reason"] == "MQTT_KEEP_ALIVE_TIMEOUT"  # raw, unchanged
    assert state.attributes["changed_at"] is None


async def test_device_tracker(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    state = hass.states.get(_entity(hass, "device_tracker", "location"))
    assert state.attributes["latitude"] == LAT_NANO / 1e9
    assert state.attributes["longitude"] == LON_NANO / 1e9
    assert state.attributes["source_type"] == "gps"
    assert state.attributes["altitude_raw"] == 1000


async def test_device_tracker_unavailable_without_coordinates(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    def no_position(vid: str, vehicle: object = None):
        state = make_state(vid)
        shadow = {k: v for k, v in state.shadow.items() if not k.startswith("con_a")}
        return type(state)(vid, state.vehicle, shadow, None, state.connectivity)

    mock_pyalso.client.get_vehicle_state.side_effect = no_position
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "device_tracker", "location")).state == STATE_UNAVAILABLE


async def test_no_coordinates_in_sensor_states(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    needles = (str(LAT_NANO), str(LON_NANO), "1.23456789", "9.87654321")
    for state in hass.states.async_all("sensor") + hass.states.async_all("binary_sensor"):
        dump = f"{state.state} {dict(state.attributes)}"
        assert not any(n in dump for n in needles), state.entity_id


async def test_dynamic_unknown_key_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    registry = er.async_get(hass)
    extra = {
        "bms_xNewThing": 5,
        "foo_bTest": True,
        "foo_sText": "a string",
        "foo_oNested": {"a": 1},
        "foo_lList": [1, 2],
        "foo_rFloat": 1.5,
        "foo_aSpyLatitude": 77,
        "foo_aLat": 78,
        "foo_xLonDeg": 79,
        "foo_xGpsFix": 80,
        "foo_bPlatform": 1,
    }
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, extra
    )
    coordinator = get_coordinator(init_integration)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    def uid(key: str) -> str:
        return f"{VEHICLE_ID}_shadow_{key}"

    for key in ("bms_xNewThing", "foo_bTest", "foo_rFloat"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid(key))
        assert entity_id, key
        reg = registry.async_get(entity_id)
        assert reg.disabled_by is None  # enabled by default
        assert reg.entity_category is EntityCategory.DIAGNOSTIC
    # A short position hint (lat, lon, gps) is skipped too, but "Platform" is not a position.
    assert registry.async_get_entity_id("sensor", DOMAIN, uid("foo_bPlatform"))
    for key in (
        "foo_sText",
        "foo_oNested",
        "foo_lList",
        "pcm_NFault",
        "foo_aSpyLatitude",
        "foo_aLat",
        "foo_xLonDeg",
        "foo_xGpsFix",
    ):
        assert registry.async_get_entity_id("sensor", DOMAIN, uid(key)) is None, key
    # Keys that have an explicit entity or are positions are never discovered.
    for key in (
        "bms_rSOCDisp",
        "con_aLatitude",
        "con_zAltitude",
        "con_tUtc",
        "pcm_vVehicleFilt",
        "hu_NModeSelectorKnob",  # now knob_position
        "bms_NState",  # now bms_state
    ):
        assert registry.async_get_entity_id("sensor", DOMAIN, uid(key)) is None, key

    count = len(registry.entities)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(registry.entities) == count  # no duplicates


async def test_dynamic_sensor_enabled_shows_raw_value(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    registry = er.async_get(hass)
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, {"bms_xNewThing": 5, "foo_bTest": True}
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_shadow_bms_xNewThing")
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None and state.state == "5"
    assert "unit_of_measurement" not in state.attributes
    assert "device_class" not in state.attributes


async def test_registered_dynamic_sensor_restored_when_key_missing(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    registry = er.async_get(hass)
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, {"bms_xNewThing": 5}
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_shadow_bms_xNewThing")
    registry.async_update_entity(entity_id, disabled_by=None)

    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(vid)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    # The entity still exists (not orphaned) but has no value while the key is absent.
    assert state is not None and state.state == STATE_UNKNOWN
