"""Sensor, binary sensor and device tracker tests."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyalso import AlsoConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import DOMAIN

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
        "charge_state": STATE_UNKNOWN,  # push only: unknown until the stream delivered data
        "charge_state_raw": "3",  # the REST shadow value, a different enum
        "power_state_raw": "1",
        "ride_mode_raw": "2",
        "fault_count": "1",
        "gps_satellites": "9",
        "last_location_update": "2024-05-01T12:00:00+00:00",
    }
    for key, value in expected.items():
        state = hass.states.get(_entity(hass, "sensor", key))
        assert state is not None and state.state == value, key

    assert (
        hass.states.get(_entity(hass, "sensor", "fault_count")).attributes["fault_level_raw"] == 2
    )
    battery = hass.states.get(_entity(hass, "sensor", "battery_level"))
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.attributes["device_class"] == "battery"


async def test_enum_sensors_are_plain_integers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for key in ("charge_state_raw", "power_state_raw", "ride_mode_raw", "gps_satellites"):
        entry = registry.async_get(_entity(hass, "sensor", key))
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        attrs = hass.states.get(entry.entity_id).attributes
        assert "options" not in attrs
        assert "device_class" not in attrs
        assert "unit_of_measurement" not in attrs


async def test_real_battery_level_disabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entry = er.async_get(hass).async_get(_entity(hass, "sensor", "battery_level_real"))
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(entry.entity_id) is None


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
        assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert reg.entity_category is EntityCategory.DIAGNOSTIC
    # Known-but-unmapped numeric keys are discovered too.
    assert registry.async_get_entity_id("sensor", DOMAIN, uid("hu_NModeSelectorKnob"))

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
    for key in ("bms_rSOCDisp", "con_aLatitude", "con_zAltitude", "con_tUtc"):
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
