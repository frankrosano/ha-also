"""Push transport tests. A FAKE transport pushes events; real grpc is never needed."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pyalso import AlsoAuthError, AlsoConnectionError, AlsoPushUnavailableError
from pyalso._wire import (
    WIRE_LEN,
    WireField,
    field_bytes,
    field_float32,
    field_string,
    field_varint,
    parse_fields,
    zigzag_encode,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import (
    DOMAIN,
    PUSH_FRESH_SECONDS,
    UNKNOWN_PACKET_HISTORY,
    VEHICLE_LIST_EVERY_N_UPDATES,
)
from custom_components.also.diagnostics import async_get_config_entry_diagnostics
from custom_components.also.sensor import CHARGE_STATE_OPTIONS

from .common import (
    BASE_TIME,
    BATTERY_SERIAL,
    PUSH_SERIAL,
    VEHICLE_ID,
    VEHICLE_ID_2,
    battery_event,
    bike_info_event,
    location_event,
    make_entry,
    make_state,
    make_vehicle,
    odometer_event,
    unknown_packet,
)
from .conftest import PyalsoMocks, get_coordinator

KM_PER_MILE = 1.609344


def _entity(hass: HomeAssistant, platform: str, key: str, vehicle_id: str = VEHICLE_ID) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{vehicle_id}_{key}")
    assert entity_id, f"no {platform} entity for {key}"
    return entity_id


def _state(hass: HomeAssistant, platform: str, key: str):
    return hass.states.get(_entity(hass, platform, key))


async def _enable(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    """Enable a disabled-by-default entity (reloads the entry, which creates a new stream)."""
    registry = er.async_get(hass)
    entity_id = _entity(hass, platform, key)
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entity_id


# ------------------------------------------------------------------------------ live updates
async def test_one_stream_per_bike_started_after_first_refresh(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert sorted(s.vehicle_id for s in mock_pyalso.streams.running) == [VEHICLE_ID, VEHICLE_ID_2]
    assert all(s.start_calls == 1 for s in mock_pyalso.streams.created)
    assert mock_pyalso.client.get_vehicle_state.await_count == 2  # the one REST refresh
    await hass.config_entries.async_unload(entry.entry_id)


async def test_push_updates_entities_without_rest_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    polls = mock_pyalso.client.get_vehicle_state.await_count
    assert _state(hass, "sensor", "minutes_until_full").state == STATE_UNKNOWN  # nothing yet
    assert _state(hass, "sensor", "battery_health").state == STATE_UNKNOWN

    stream.emit(battery_event(55, minutes=42, health=97, limit=80))
    stream.emit(location_event())
    stream.emit(odometer_event(123_500))
    await hass.async_block_till_done()

    assert float(_state(hass, "sensor", "battery_level").state) == 55
    assert float(_state(hass, "sensor", "charge_limit").state) == 80
    assert _state(hass, "sensor", "minutes_until_full").state == "42"
    assert _state(hass, "sensor", "battery_health").state == "97"
    tracker = _state(hass, "device_tracker", "location")
    assert tracker.attributes["latitude"] == 2.2
    assert tracker.attributes["longitude"] == -3.3
    assert float(_state(hass, "sensor", "odometer").state) == pytest.approx(
        123_500 * KM_PER_MILE, rel=1e-4
    )
    assert mock_pyalso.client.get_vehicle_state.await_count == polls  # no REST poll happened


async def test_minutes_until_full_stays_unknown_when_absent(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    # Other packets do not carry the value: it stays unknown, nothing is made up.
    stream.emit(bike_info_event())
    stream.emit(odometer_event(123_500))
    # An un-stamped battery packet without the field is not read as "zero minutes" either. (It also
    # has no source time, so its level cannot outrank the timestamped REST value: the REST
    # reading stays.)
    stream.emit(battery_event(60, minutes=None, health=None, at=None))
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 87.5
    assert _state(hass, "sensor", "minutes_until_full").state == STATE_UNKNOWN
    assert _state(hass, "sensor", "battery_health").state == STATE_UNKNOWN
    # In a stamped packet proto3 omits zero, and pyalso documents that as 0 minutes (full).
    stream.emit(battery_event(90, minutes=None, health=None, at=BASE_TIME + 100))
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "minutes_until_full").state == "0"


async def test_push_does_not_reset_the_poll_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    coordinator = get_coordinator(init_integration)
    stream = mock_pyalso.streams.latest()
    with (
        patch.object(coordinator, "async_set_updated_data") as set_data,
        patch.object(coordinator, "_schedule_refresh") as schedule,
    ):
        for level in range(50, 55):
            stream.emit(battery_event(level, at=BASE_TIME + 100 + level))
        await hass.async_block_till_done()
    set_data.assert_not_called()
    schedule.assert_not_called()
    assert float(_state(hass, "sensor", "battery_level").state) == 54


async def test_stale_push_does_not_overwrite_newer_data(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    coordinator = get_coordinator(init_integration)
    stream.emit(battery_event(40, at=BASE_TIME + 200))
    stream.emit(battery_event(99, at=BASE_TIME + 100))  # a cached snapshot from the cloud
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 40

    # A REST report that is older than the pushed data does not win either ...
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 40

    # ... and a newer one does.
    mock_pyalso.rest_state = lambda vid: make_state(
        vid, {"bms_rSOCDisp": 70.0, "con_tUtc": BASE_TIME + 500}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 70


async def test_odometer_never_goes_down(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    stream.emit(odometer_event(100))  # an old cached value
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "odometer").state) == pytest.approx(
        123_456 * KM_PER_MILE, rel=1e-4
    )


# ---------------------------------------------------------------------- connection state
async def test_push_connected_sensor_follows_the_stream(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    registry = er.async_get(hass)
    entry = registry.async_get(_entity(hass, "binary_sensor", "push_connected"))
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is EntityCategory.DIAGNOSTIC

    entity_id = await _enable(hass, init_integration, "binary_sensor", "push_connected")
    stream = mock_pyalso.streams.latest()
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == "connectivity"
    assert state.attributes["last_push"] is None
    assert state.attributes["polling_only"] is False

    stream.status(True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON

    stream.emit(battery_event())
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["last_push"] is not None

    stream.status(False, "stream_ended")
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["last_status"] == "stream_ended"
    assert state.attributes["failed_attempts"] == 1
    assert state.attributes["last_push"] is not None  # remembered while disconnected

    stream.status(True)  # reconnected
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["failed_attempts"] == 0


async def test_keepalive_replies_do_not_count_as_push(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    from pyalso import KeepAliveEvent

    entity_id = await _enable(hass, init_integration, "binary_sensor", "push_connected")
    mock_pyalso.streams.latest().emit(
        KeepAliveEvent(feature=0x40, command=1, status=0, timestamp=BASE_TIME, payload=b"")
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["last_push"] is None


async def test_repeated_failures_log_one_warning_per_outage(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = mock_pyalso.streams.latest()
    caplog.set_level(logging.INFO, logger="custom_components.also.coordinator")

    def warnings() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "Live updates" in r.getMessage()
        ]

    for _ in range(8):  # eight failed retries ...
        stream.status(False, "rpc_error:UNAVAILABLE")
    assert len(warnings()) == 1  # ... one warning
    assert VEHICLE_ID not in warnings()[0]

    stream.status(True)
    assert any("back" in r.getMessage() for r in caplog.records)
    for _ in range(5):
        stream.status(False, "rpc_error:UNAVAILABLE")
    assert len(warnings()) == 2  # a new outage warns again, once
    await hass.async_block_till_done()
    # Polling is unaffected and the data stays available.
    assert _state(hass, "sensor", "battery_level").state == "87.5"


async def test_auth_error_from_stream_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    stream.status(False, "auth_failed", error=AlsoAuthError(401, None, "expired"), fatal=True)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id
    # Polling still works, so the bike's data is not thrown away.
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE
    # A second fatal report does not start a second flow.
    stream.status(False, "auth_failed", error=AlsoAuthError(401, None, "expired"), fatal=True)
    await hass.async_block_till_done()
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


# ------------------------------------------------------------------------------ lifecycle
async def test_unload_stops_streams_and_listeners(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    assert stream.running
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert stream.stop_calls == 1
    assert not stream.running
    assert stream.listener_count == 0
    assert not mock_pyalso.streams.running
    # Events after unload are ignored (nothing is subscribed any more).
    stream.emit(battery_event(1))


async def test_homeassistant_stop_stops_streams(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert stream.stop_calls == 1
    assert not mock_pyalso.streams.running


async def test_reload_replaces_streams_without_leaking(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    first = mock_pyalso.streams.latest()
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    second = mock_pyalso.streams.latest()
    assert second is not first
    assert not first.running
    assert second.running
    assert mock_pyalso.streams.running == [second]


async def test_streams_follow_the_vehicle_list(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    coordinator = get_coordinator(init_integration)
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    for _ in range(VEHICLE_LIST_EVERY_N_UPDATES):
        await coordinator.async_refresh()
    assert mock_pyalso.streams.latest(VEHICLE_ID_2).running
    assert mock_pyalso.streams.latest(VEHICLE_ID).start_calls == 1  # not restarted

    mock_pyalso.client.list_all_vehicles.return_value = [make_vehicle(VEHICLE_ID)]
    for _ in range(VEHICLE_LIST_EVERY_N_UPDATES):
        await coordinator.async_refresh()
    assert not mock_pyalso.streams.latest(VEHICLE_ID_2).running
    assert mock_pyalso.streams.latest(VEHICLE_ID_2).stop_calls == 1
    assert mock_pyalso.streams.latest(VEHICLE_ID).running


# --------------------------------------------------------------------- fallback to polling
@pytest.mark.parametrize("error", [AlsoPushUnavailableError("no grpc"), RuntimeError("boom")])
async def test_falls_back_to_polling_when_push_is_unavailable(
    hass: HomeAssistant,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    mock_pyalso.streams.start_error = error
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = get_coordinator(entry)
    assert not coordinator.push_enabled
    assert coordinator.push_status(VEHICLE_ID).unavailable
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "Live updates" in r.message
    ]
    assert len(warnings) == 1
    assert len(mock_pyalso.streams.created) == 1  # it did not retry for the other bike
    assert not mock_pyalso.streams.running

    # Polling keeps the entities alive.
    assert _state(hass, "sensor", "battery_level").state == "87.5"
    mock_pyalso.rest_state = lambda vid: make_state(vid, {"bms_rSOCDisp": 12.5})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state == "12.5"
    assert len(mock_pyalso.streams.created) == 1  # no stream restarts on later polls
    await hass.config_entries.async_unload(entry.entry_id)


async def test_push_connected_sensor_reports_polling_only(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.streams.start_error = AlsoPushUnavailableError("no grpc")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = await _enable(hass, entry, "binary_sensor", "push_connected")
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["polling_only"] is True
    await hass.config_entries.async_unload(entry.entry_id)


# ----------------------------------------------------------------------------- entities
async def test_odometer_unit_and_device_class_metric(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entity_id = _entity(hass, "sensor", "odometer")
    state = hass.states.get(entity_id)
    assert state.attributes["device_class"] == "distance"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "km"  # Home Assistant converts miles
    assert float(state.state) == pytest.approx(123_456 * KM_PER_MILE, rel=1e-4)
    assert "unverified" not in (state.name or "").lower()


async def test_odometer_unit_us_customary(hass: HomeAssistant, mock_pyalso: PyalsoMocks) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = _state(hass, "sensor", "odometer")
    assert state.attributes["unit_of_measurement"] == "mi"
    assert float(state.state) == 123_456
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_odometer_unique_id_is_migrated(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{VEHICLE_ID}_odometer_raw", config_entry=entry
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_odometer") == old.entity_id
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_odometer_raw") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_charge_state_enum_sensor_follows_the_push_stream(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    # No push data yet: the enum sensor is unknown (the REST value is NOT used for it) ...
    assert _state(hass, "sensor", "charge_state").state == STATE_UNKNOWN
    # ... while the raw REST shadow value is its own diagnostic sensor.
    shadow = _state(hass, "sensor", "charge_state_raw")
    assert shadow.state == "3"
    assert "shadow, raw" in shadow.name.lower()
    assert (
        er.async_get(hass).async_get(shadow.entity_id).entity_category is EntityCategory.DIAGNOSTIC
    )

    # Unplugged and idle: the stream says 1 (not_charging) while the REST shadow still reads 3.
    stream.emit(battery_event(charge_state=1, at=BASE_TIME + 100))
    await hass.async_block_till_done()
    state = _state(hass, "sensor", "charge_state")
    assert state.state == "not_charging"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == CHARGE_STATE_OPTIONS
    assert state.attributes["charging_state_raw"] == 1
    assert _state(hass, "sensor", "charge_state_raw").state == "3"  # untouched by the stream

    # Fully charged: both happen to read 3, but they stay independent values.
    stream.emit(battery_event(charge_state=3, at=BASE_TIME + 200))
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "charge_state").state == "fully_charged"
    mock_pyalso.rest_state = lambda vid: make_state(
        vid, {"con_NChargeState": 0, "con_tUtc": BASE_TIME + 50}
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "charge_state").state == "fully_charged"  # REST never touches it
    # The REST report is the newest source for ITS key: the pushed value never shadows it.
    assert _state(hass, "sensor", "charge_state_raw").state == "0"


async def test_charge_state_rest_value_alone_never_drives_the_enum_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.rest_state = lambda vid: make_state(
        vid, {"con_NChargeState": 0, "con_tUtc": BASE_TIME + 500}
    )
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "charge_state_raw").state == "0"
    assert _state(hass, "sensor", "charge_state").state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("number", "expected"), [(2, "unknown_2"), (4, "unknown_4"), (7, "unknown_7")]
)
async def test_unverified_or_new_charge_states_fall_back_to_unknown_n(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    number: int,
    expected: str,
) -> None:
    mock_pyalso.streams.latest().emit(battery_event(charge_state=number, at=BASE_TIME + 300))
    await hass.async_block_till_done()
    state = _state(hass, "sensor", "charge_state")
    assert state.state == expected  # no exception, no made-up name
    assert expected in state.attributes["options"]
    assert state.attributes["options"][:2] == ["not_charging", "fully_charged"]
    assert state.attributes["charging_state_raw"] == number


async def test_device_info_and_firmware_from_the_stream(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    devices = dr.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, VEHICLE_ID)})
    assert device.model == "TEST-MODEL"
    assert device.sw_version is None
    assert _state(hass, "sensor", "firmware_version").state == STATE_UNKNOWN

    mock_pyalso.streams.latest().emit(bike_info_event("Test Model X", "9.8.7"))
    await hass.async_block_till_done()

    device = devices.async_get_device(identifiers={(DOMAIN, VEHICLE_ID)})
    assert device.model == "Test Model X"
    assert device.sw_version == "9.8.7"
    firmware = _state(hass, "sensor", "firmware_version")
    assert firmware.state == "9.8.7"
    registry = er.async_get(hass)
    assert registry.async_get(firmware.entity_id).entity_category is EntityCategory.DIAGNOSTIC
    # The serial numbers the stream reports are not exposed by any entity.
    for state in hass.states.async_all():
        assert PUSH_SERIAL not in f"{state.state} {dict(state.attributes)}"
        assert BATTERY_SERIAL not in f"{state.state} {dict(state.attributes)}"


# ---------------------------------------------------------------------------- diagnostics
SECRET_TEXT = "SECRETTEXT-ABC"
NESTED_SECRET = "NESTEDSECRET"
BIG = 1_234_567_890


def _unknown_payload() -> bytes:
    return (
        field_string(1, SECRET_TEXT)
        + field_varint(2, zigzag_encode(BIG))
        + field_varint(3, 42)
        + field_bytes(4, field_string(1, NESTED_SECRET) + field_varint(2, 7))
        + field_float32(5, 1.5)
    )


async def test_diagnostics_redacts_unknown_packets_and_push_values(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    payload = _unknown_payload()
    stream.emit(bike_info_event())
    stream.emit(battery_event(at=BASE_TIME + 100))
    stream.emit(unknown_packet(payload))
    stream.emit(unknown_packet(b"\xff\xff\xff", feature=0x78, command=0x0106))  # undecodable
    stream.emit(
        battery_event(
            at=BASE_TIME + 101, unknown_fields=(WireField(12, WIRE_LEN, b"SECRETEXTRA-1"),)
        )
    )
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    dump = json.dumps(diag, default=str)

    for secret in (
        PUSH_SERIAL,
        BATTERY_SERIAL,
        SECRET_TEXT,
        NESTED_SECRET,
        "SECRETEXTRA-1",
        SECRET_TEXT.encode().hex(),
        NESTED_SECRET.encode().hex(),
        b"SECRETEXTRA-1".hex(),
        str(zigzag_encode(BIG)),
        str(BIG),
    ):
        assert secret not in dump, secret

    assert diag["unknown_packet_count"] == 3
    decoded, undecodable, extra_fields = diag["unknown_packets"]

    assert decoded["kind"] == "unknown_packet"
    assert decoded["feature"] == "0x77"
    assert decoded["command"] == "0x0105"
    assert decoded["status"] == "0xff"
    assert decoded["length"] == len(payload)
    fields = {f.number: f for f in parse_fields(bytes.fromhex(decoded["payload_hex_redacted"]))}
    assert fields[1].value == b"x" * len(SECRET_TEXT)  # text masked, same length
    assert fields[2].value == 0  # the coordinate-sized number is zeroed
    assert fields[3].value == 42  # small numbers survive: that is what we want to learn from
    nested = {f.number: f for f in parse_fields(fields[4].value)}
    assert nested[1].value == b"x" * len(NESTED_SECRET)
    assert nested[2].value == 7
    assert 5 in fields

    assert undecodable["payload_hex_redacted"] is None  # only feature/command/status/length
    assert undecodable["length"] == 3
    assert undecodable["feature"] == "0x78"

    assert extra_fields["kind"] == "unknown_fields"
    assert extra_fields["field_numbers"] == [12]

    # Push-only values are in the dump with serials redacted; the model is not secret.
    assert diag["push_enabled"] is True
    assert diag["push"][0]["last_event"] is not None
    pushed = diag["vehicles"][0]["push"]
    assert pushed["battery_serial"] == "**REDACTED**"
    assert pushed["vehicle_serial"] == "**REDACTED**"
    assert pushed["model_name"] == "Test Model X"


async def test_unknown_packets_are_a_rolling_window(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    for n in range(UNKNOWN_PACKET_HISTORY + 5):
        stream.emit(unknown_packet(field_varint(1, n), command=0x0200 + n))
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["unknown_packet_count"] == UNKNOWN_PACKET_HISTORY + 5
    assert len(diag["unknown_packets"]) == UNKNOWN_PACKET_HISTORY
    assert diag["unknown_packets"][-1]["command"] == f"{0x0200 + UNKNOWN_PACKET_HISTORY + 4:#06x}"
    assert diag["unknown_packets"][0]["command"] == f"{0x0205:#06x}"


async def test_unknown_packet_is_logged_without_payload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.also.coordinator")
    mock_pyalso.streams.latest().emit(unknown_packet(_unknown_payload()))
    lines = [r.getMessage() for r in caplog.records if "Unknown stream data" in r.getMessage()]
    assert lines == [
        f"Unknown stream data 0x77/0x0105 status 0xff ({len(_unknown_payload())} bytes)"
    ]
    assert SECRET_TEXT not in caplog.text
    assert SECRET_TEXT.encode().hex() not in caplog.text


# ------------------------------------------------------------- real pyalso decoders / API
async def test_events_decoded_by_real_pyalso_drive_the_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    from pyalso.events import decode_raw
    from pyalso.protocol import Packet

    battery = (
        field_varint(1, 1)
        + field_varint(4, 1)
        + field_varint(5, 66)
        + field_varint(6, 30)
        + field_varint(7, 85)
        + field_varint(8, 95)
        + field_varint(9, BASE_TIME + 150)
    )
    stream = mock_pyalso.streams.latest()
    stream.emit(decode_raw(Packet(0x04, 0x0101, 0xFF, BASE_TIME, field_bytes(1, battery)).encode()))
    stream.emit(decode_raw(Packet(0x77, 0x0105, 0xFF, BASE_TIME, _unknown_payload()).encode()))
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 66
    assert float(_state(hass, "sensor", "charge_limit").state) == 85
    assert _state(hass, "sensor", "minutes_until_full").state == "30"
    assert _state(hass, "sensor", "battery_health").state == "95"
    assert _state(hass, "sensor", "charge_state").state == "not_charging"
    assert _state(hass, "sensor", "charge_state_raw").state == "3"  # REST shadow, separate enum
    assert get_coordinator(init_integration).unknown_packet_count == 1


def test_alsostream_provides_the_interface_the_coordinator_uses() -> None:
    from unittest.mock import MagicMock

    from pyalso import AlsoStream

    stream = AlsoStream(MagicMock(), VEHICLE_ID)  # building it needs no grpcio
    for name in ("start", "stop", "add_event_listener", "add_connection_listener"):
        assert callable(getattr(stream, name))
    assert callable(stream.add_event_listener(lambda event: None))
    assert callable(stream.add_connection_listener(lambda status: None))


def test_manifest_matches_the_push_setup() -> None:
    """The two release trains are independent: the integration version is its own, but the library
    tag the manifest requires must be the pyalso version this suite runs against."""
    import pathlib

    import pyalso

    manifest = json.loads(
        (pathlib.Path(__file__).parents[1] / "custom_components/also/manifest.json").read_text()
    )
    assert manifest["iot_class"] == "cloud_push"
    # No spaces: hassfest rejects "name @ url". The tag is the installed library version.
    assert manifest["requirements"] == [
        "pyalso[push]@https://github.com/frankrosano/pyalso/archive/refs/tags/"
        f"v{pyalso.__version__}.zip"
    ]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])  # its own version, not pyalso's


def test_hacs_json_declares_a_minimum_home_assistant_version() -> None:
    import pathlib

    hacs = json.loads((pathlib.Path(__file__).parents[1] / "hacs.json").read_text())
    # 2025.12.0 is the first core release with UpdateFailed(retry_after=...) (checked against the
    # 2025.11.0 and 2025.12.0 sources); every other API used exists there as well.
    assert hacs["homeassistant"] == "2025.12.0"


# ----------------------------------------------------------- availability from either source
async def _fail_rest(hass: HomeAssistant, entry: MockConfigEntry, mock_pyalso: PyalsoMocks) -> None:
    mock_pyalso.client.get_vehicle_state.side_effect = AlsoConnectionError("rest is down")
    await get_coordinator(entry).async_refresh()
    await hass.async_block_till_done()
    assert get_coordinator(entry).last_update_success is False


async def test_entities_stay_available_when_rest_fails_but_push_is_connected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    stream.status(True)
    stream.emit(battery_event(61, at=BASE_TIME + 100))
    await _fail_rest(hass, init_integration, mock_pyalso)
    for platform, key in (
        ("sensor", "battery_level"),
        ("sensor", "odometer"),
        ("device_tracker", "location"),
        ("binary_sensor", "connected"),
    ):
        assert _state(hass, platform, key).state != STATE_UNAVAILABLE, key
    assert float(_state(hass, "sensor", "battery_level").state) == 61
    # Live data keeps flowing and keeps the entities current while REST is down.
    stream.emit(battery_event(60, at=BASE_TIME + 220))
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor", "battery_level").state) == 60


async def test_entities_go_unavailable_when_rest_fails_and_push_is_not_live(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    await _fail_rest(hass, init_integration, mock_pyalso)  # no stream status, no push data
    assert _state(hass, "sensor", "battery_level").state == STATE_UNAVAILABLE
    assert _state(hass, "device_tracker", "location").state == STATE_UNAVAILABLE


async def test_recent_push_keeps_entities_available_for_a_while_after_a_disconnect(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    freezer,  # type: ignore[no-untyped-def]
) -> None:
    coordinator = get_coordinator(init_integration)
    working_rest = mock_pyalso.client.get_vehicle_state.side_effect
    stream = mock_pyalso.streams.latest()
    stream.status(True)
    stream.emit(battery_event(61, at=BASE_TIME + 100))
    stream.status(False, "stream_ended")  # disconnected, but data arrived a moment ago
    await _fail_rest(hass, init_integration, mock_pyalso)
    assert coordinator.has_recent_push(VEHICLE_ID)
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE

    freezer.tick(timedelta(seconds=PUSH_FRESH_SECONDS - 5))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE

    freezer.tick(timedelta(seconds=10))  # the last push is now older than 3 cadences + margin
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert not coordinator.has_recent_push(VEHICLE_ID)
    assert _state(hass, "sensor", "battery_level").state == STATE_UNAVAILABLE

    # A working REST poll makes everything available again.
    mock_pyalso.client.get_vehicle_state.side_effect = working_rest
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "battery_level").state != STATE_UNAVAILABLE


async def test_a_failing_rest_poll_is_logged_once_not_per_retry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_pyalso.streams.latest().status(True)
    caplog.clear()
    mock_pyalso.client.get_vehicle_state.side_effect = AlsoConnectionError("rest is down")
    coordinator = get_coordinator(init_integration)
    for _ in range(4):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    errors = [
        r for r in caplog.records if r.levelno >= logging.ERROR and "Error fetching" in r.message
    ]
    assert len(errors) == 1


# ------------------------------------------------------------------ last location update
async def test_last_location_update_follows_a_pushed_fix(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    sensor = "last_location_update"
    assert _state(hass, "sensor", sensor).state == "2024-05-01T12:00:00+00:00"  # REST location
    rest_time = int(datetime(2024, 5, 1, 12, tzinfo=UTC).timestamp())
    stream = mock_pyalso.streams.latest()

    stream.emit(location_event(at=rest_time - 3600))  # an older cached fix
    await hass.async_block_till_done()
    assert _state(hass, "sensor", sensor).state == "2024-05-01T12:00:00+00:00"  # never moves back

    stream.emit(location_event(at=rest_time + 90))  # a newer live fix
    await hass.async_block_till_done()
    expected = datetime.fromtimestamp(rest_time + 90, UTC).isoformat()
    assert _state(hass, "sensor", sensor).state == expected
    assert _state(hass, "device_tracker", "location").attributes["latitude"] == 2.2

    # A later REST poll with an older location timestamp does not pull it back either.
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", sensor).state == expected


# ------------------------------------------------------------- logging per bike / failures
def _live_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Live updates" in r.getMessage()
    ]


async def test_fewer_than_three_failures_stay_silent(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = mock_pyalso.streams.latest()
    caplog.set_level(logging.INFO, logger="custom_components.also.coordinator")
    stream.status(False, "rpc_error:UNAVAILABLE")
    stream.status(False, "rpc_error:UNAVAILABLE")
    stream.status(True)  # recovered before the third failure
    assert _live_warnings(caplog) == []
    assert not any("are back" in r.getMessage() for r in caplog.records)  # nothing to announce


async def test_non_auth_fatal_status_warns_once_and_does_not_start_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = mock_pyalso.streams.latest()
    caplog.set_level(logging.INFO, logger="custom_components.also.coordinator")
    crash = RuntimeError("supervisor bug")
    stream.status(False, "internal_error", error=crash, fatal=True)
    stream.status(False, "internal_error", error=crash, fatal=True)
    await hass.async_block_till_done()
    assert len(_live_warnings(caplog)) == 1
    assert "internal_error" in _live_warnings(caplog)[0]
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert get_coordinator(init_integration).push_status(VEHICLE_ID).unavailable
    assert _state(hass, "sensor", "battery_level").state == "87.5"  # polling continues


async def test_outage_warnings_are_tracked_per_bike(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks, caplog: pytest.LogCaptureFixture
) -> None:
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    caplog.set_level(logging.INFO, logger="custom_components.also.coordinator")
    one = mock_pyalso.streams.latest(VEHICLE_ID)
    two = mock_pyalso.streams.latest(VEHICLE_ID_2)

    for _ in range(4):
        one.status(False, "rpc_error:UNAVAILABLE")
        two.status(False, "rpc_error:UNAVAILABLE")
    assert len(_live_warnings(caplog)) == 2  # one warning per bike

    one.status(True)  # bike one is back; bike two is still down
    for _ in range(4):
        two.status(False, "rpc_error:UNAVAILABLE")
    assert len(_live_warnings(caplog)) == 2  # bike two's outage was already reported
    assert sum("are back" in r.getMessage() for r in caplog.records) == 1

    for _ in range(4):
        one.status(False, "rpc_error:UNAVAILABLE")
    assert len(_live_warnings(caplog)) == 3  # bike one's NEW outage warns again
    await hass.config_entries.async_unload(entry.entry_id)


async def test_start_failure_on_the_second_bike_stops_the_first_and_disables_push(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks, caplog: pytest.LogCaptureFixture
) -> None:
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    original_create = mock_pyalso.streams.create

    def create(vehicle_id: str):  # type: ignore[no-untyped-def]
        stream = original_create(vehicle_id)
        if vehicle_id == VEHICLE_ID_2:
            stream.start_error = RuntimeError("boom")
        return stream

    mock_pyalso.streams.create = create  # type: ignore[method-assign]
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first = mock_pyalso.streams.latest(VEHICLE_ID)
    second = mock_pyalso.streams.latest(VEHICLE_ID_2)
    assert first.start_calls == 1 and second.start_calls == 1
    assert not first.running and first.stop_calls == 1  # the running stream was stopped again
    assert first.listener_count == 0 and second.listener_count == 0
    coordinator = get_coordinator(entry)
    assert not coordinator.push_enabled
    assert len(_live_warnings(caplog)) == 1
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", "battery_level").state == "87.5"
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ unknown packet window
async def test_repeated_unknown_fields_are_collapsed_and_counted(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    stream = mock_pyalso.streams.latest()
    rare = unknown_packet(field_varint(1, 5), feature=0x78, command=0x0001)
    stream.emit(rare)
    recurring = (WireField(12, WIRE_LEN, b"abc"),)
    for n in range(UNKNOWN_PACKET_HISTORY * 2):  # a recurring unknown field on every battery push
        stream.emit(battery_event(50, at=BASE_TIME + 100 + n, unknown_fields=recurring))
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["unknown_packet_count"] == 1 + UNKNOWN_PACKET_HISTORY * 2
    first, second = diag["unknown_packets"]
    assert first["command"] == "0x0001" and first["count"] == 1  # the rare packet survived
    assert second["kind"] == "unknown_fields" and second["field_numbers"] == [12]
    assert second["count"] == UNKNOWN_PACKET_HISTORY * 2


async def test_diagnostics_show_both_charge_states_side_by_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["push"][0]["charging_state_raw"] is None
    assert diag["push"][0]["shadow_charge_state_raw"] == 3
    mock_pyalso.streams.latest().emit(battery_event(charge_state=1, at=BASE_TIME + 100))
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["push"][0]["charging_state_raw"] == 1
    assert diag["push"][0]["shadow_charge_state_raw"] == 3
