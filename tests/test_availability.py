"""Entity availability from either source, and the timer that expires push freshness."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyalso import AlsoConnectionError, KeepAliveEvent
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.also.const import CONF_SCAN_INTERVAL, DOMAIN, PUSH_FRESH_SECONDS

from .common import BASE_TIME, VEHICLE_ID, battery_event, make_entry
from .conftest import PyalsoMocks, get_coordinator


def _state(hass: HomeAssistant, platform: str, key: str):  # type: ignore[no-untyped-def]
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{VEHICLE_ID}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id)


async def _fail_rest(hass: HomeAssistant, entry: MockConfigEntry, mocks: PyalsoMocks) -> None:
    mocks.client.get_vehicle_state.side_effect = AlsoConnectionError("rest is down")
    await get_coordinator(entry).async_refresh()
    await hass.async_block_till_done()
    assert get_coordinator(entry).last_update_success is False


async def test_a_connected_but_data_silent_stream_does_not_keep_entities_available(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    """Regression: ``connected`` alone used to keep frozen values available indefinitely while
    REST was down, because keep-alive replies keep a silent stream 'connected'."""
    working_rest = mock_pyalso.client.get_vehicle_state.side_effect
    stream = mock_pyalso.streams.latest()
    stream.status(True)
    stream.emit(KeepAliveEvent(feature=0x40, command=1, status=0, timestamp=BASE_TIME, payload=b""))
    await _fail_rest(hass, init_integration, mock_pyalso)

    coordinator = get_coordinator(init_integration)
    assert coordinator.push_status(VEHICLE_ID).connected  # transport is healthy ...
    assert not coordinator.has_recent_push(VEHICLE_ID)  # ... but no data ever arrived
    for platform, key in (("sensor", "battery_level"), ("device_tracker", "location")):
        assert _state(hass, platform, key).state == STATE_UNAVAILABLE, key

    # The raw connection state stays visible on the diagnostic sensor.
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{VEHICLE_ID}_push_connected"
    )
    assert entity_id
    registry.async_update_entity(entity_id, disabled_by=None)
    mock_pyalso.client.get_vehicle_state.side_effect = working_rest  # (the reload polls once)
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    mock_pyalso.streams.latest().status(True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON


async def test_data_events_keep_entities_available_while_rest_is_down(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    stream.emit(battery_event(61, at=BASE_TIME + 100))
    await _fail_rest(hass, init_integration, mock_pyalso)
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE


async def test_availability_flips_at_the_freshness_deadline_without_any_notification(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks, freezer
) -> None:  # type: ignore[no-untyped-def]
    """Regression: nothing re-evaluated freshness as time passed; entities stayed available until
    the next unrelated update (up to the 1 h poll interval). The real timer is used here: the
    clock moves and Home Assistant fires due timers, and NO listener is notified by hand."""
    entry = make_entry(options={CONF_SCAN_INTERVAL: 3600})  # the poll timer cannot interfere
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    stream = mock_pyalso.streams.latest()
    stream.emit(battery_event(61, at=BASE_TIME + 100))
    await _fail_rest(hass, entry, mock_pyalso)
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE

    freezer.tick(timedelta(seconds=PUSH_FRESH_SECONDS - 10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE  # not yet

    freezer.tick(timedelta(seconds=20))  # now past the deadline
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state == STATE_UNAVAILABLE
    assert _state(hass, "device_tracker", "location").state == STATE_UNAVAILABLE
    await hass.config_entries.async_unload(entry.entry_id)


async def test_each_new_data_event_pushes_the_deadline_back(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks, freezer
) -> None:  # type: ignore[no-untyped-def]
    entry = make_entry(options={CONF_SCAN_INTERVAL: 3600})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    stream = mock_pyalso.streams.latest()
    stream.emit(battery_event(61, at=BASE_TIME + 100))
    await _fail_rest(hass, entry, mock_pyalso)

    freezer.tick(timedelta(seconds=PUSH_FRESH_SECONDS - 30))
    async_fire_time_changed(hass)
    stream.emit(battery_event(60, at=BASE_TIME + 200))  # fresh data re-arms the timer
    freezer.tick(timedelta(seconds=60))  # past the FIRST deadline, not past the new one
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE

    freezer.tick(timedelta(seconds=PUSH_FRESH_SECONDS))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state == STATE_UNAVAILABLE
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_the_expiry_timers(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.streams.latest().emit(battery_event(61, at=BASE_TIME + 100))
    coordinator = get_coordinator(init_integration)
    assert coordinator._expiry_timers  # armed by the data event
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert coordinator._expiry_timers == {}
