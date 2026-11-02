"""Setup, coordinator and token-rotation tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pyalso import (
    AlsoApiError,
    AlsoConnectionError,
    AlsoRateLimitError,
    AlsoSessionExpiredError,
    Tokens,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    VEHICLE_LIST_EVERY_N_UPDATES,
)

from .common import (
    ACCESS_TOKEN,
    BASE_TIME,
    EMAIL,
    REFRESH_TOKEN,
    VEHICLE_ID,
    VEHICLE_ID_2,
    battery_event,
    make_entry,
    make_vehicle,
)
from .conftest import PyalsoMocks, get_coordinator


async def test_setup_populates_two_vehicles(hass: HomeAssistant, mock_pyalso: PyalsoMocks) -> None:
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
    assert set(coordinator.data) == {VEHICLE_ID, VEHICLE_ID_2}
    # The stored tokens are handed to AlsoAuth.
    tokens = mock_pyalso.auth_cls.call_args.kwargs["tokens"]
    assert tokens.access_token == ACCESS_TOKEN
    assert tokens.refresh_token == REFRESH_TOKEN
    # One device per bike.
    assert len(er.async_get(hass).entities) > 0
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_scan_interval_from_options(hass: HomeAssistant, mock_pyalso: PyalsoMocks) -> None:
    entry = make_entry(options={CONF_SCAN_INTERVAL: 600})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert get_coordinator(entry).update_interval == timedelta(seconds=600)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_default_scan_interval(init_integration: MockConfigEntry) -> None:
    assert get_coordinator(init_integration).update_interval == timedelta(seconds=300)


async def test_options_change_reloads(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(init_integration, options={CONF_SCAN_INTERVAL: 900})
    await hass.async_block_till_done()
    assert get_coordinator(init_integration).update_interval == timedelta(seconds=900)


async def test_session_expired_starts_reauth(hass: HomeAssistant, mock_pyalso: PyalsoMocks) -> None:
    mock_pyalso.client.get_vehicle_state.side_effect = AlsoSessionExpiredError(498, None, "gone")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_incomplete_stored_credentials_start_reauth(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", data={CONF_EMAIL: EMAIL})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize("expires_at", [None, "not-a-number"])
async def test_invalid_stored_expiry_starts_reauth(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks, expires_at: object
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_EXPIRES_AT: expires_at})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize(
    "error",
    [AlsoRateLimitError(retry_after=60), AlsoConnectionError(), AlsoApiError(503, None, "down")],
)
async def test_update_errors_make_entities_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    error: Exception,
) -> None:
    unique_id = f"{VEHICLE_ID}_battery_level"
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id
    assert hass.states.get(entity_id).state == "87.5"

    mock_pyalso.client.get_vehicle_state.side_effect = error
    coordinator = get_coordinator(init_integration)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert hass.states.get(entity_id).state == "unavailable"
    if isinstance(error, AlsoRateLimitError):
        assert coordinator.last_exception.retry_after == 60


async def test_session_expired_on_refresh_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    mock_pyalso.client.get_vehicle_state.side_effect = AlsoSessionExpiredError(498, None, "gone")
    await get_coordinator(init_integration).async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_vehicle_list_refreshed_every_n_updates(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    coordinator = get_coordinator(init_integration)
    list_mock = mock_pyalso.client.list_all_vehicles
    assert list_mock.await_count == 1  # first update
    for _ in range(VEHICLE_LIST_EVERY_N_UPDATES - 1):
        await coordinator.async_refresh()
    assert list_mock.await_count == 1
    await coordinator.async_refresh()  # the Nth update after the first
    assert list_mock.await_count == 2
    assert mock_pyalso.client.get_vehicle_state.await_count == VEHICLE_LIST_EVERY_N_UPDATES + 1


async def test_token_rotation_is_persisted_without_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    persist = mock_pyalso.auth_cls.call_args.kwargs["token_update_callback"]
    assert mock_pyalso.auth_cls.call_count == 1

    for n in (1, 2):  # EVERY rotation must be written, not just the first
        await persist(Tokens(f"new-access-{n}", f"new-refresh-{n}", f"new-id-{n}", 5_000.0 + n))
        await hass.async_block_till_done()
        assert init_integration.data[CONF_ACCESS_TOKEN] == f"new-access-{n}"
        assert init_integration.data[CONF_REFRESH_TOKEN] == f"new-refresh-{n}"
        assert init_integration.data[CONF_ID_TOKEN] == f"new-id-{n}"
        assert init_integration.data[CONF_EXPIRES_AT] == 5_000.0 + n
        assert init_integration.data[CONF_EMAIL] == EMAIL

    assert init_integration.state is ConfigEntryState.LOADED
    assert mock_pyalso.auth_cls.call_count == 1  # token updates did not reload the entry


async def test_apply_push_update_merges_and_notifies(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    coordinator = get_coordinator(init_integration)
    calls = []
    unsub = coordinator.async_add_listener(lambda: calls.append(1))
    assert coordinator.async_apply_push_update(
        VEHICLE_ID,
        {"bms_rSOCDisp": 50.0, "pcm_NFault": {"NFaultCount": 0}},
        push={"minutes_till_full": 12},
        updated_at=BASE_TIME + 10,
    )
    state = coordinator.data[VEHICLE_ID]
    assert state.battery_level == 50.0
    assert state.minutes_till_full == 12
    assert state.fault_count == 0
    assert state.fault_level_raw == 2  # untouched nested key survives the merge
    assert state.odometer_raw == 123456
    assert calls
    # An older update changes nothing and does not notify.
    calls.clear()
    assert not coordinator.async_apply_push_update(
        VEHICLE_ID, {"bms_rSOCDisp": 1.0}, updated_at=BASE_TIME - 10
    )
    assert coordinator.data[VEHICLE_ID].battery_level == 50.0
    assert not calls
    # Unknown vehicles are ignored.
    before = coordinator.data
    assert not coordinator.async_apply_push_update("VHC-UNKNOWN", {"bms_rSOCDisp": 1})
    assert coordinator.data is before
    unsub()


async def test_unload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED


# ------------------------------------------------ registry cleanup on setup (0.3.0, 0.4.0)
REMOVED_0_3_0 = ("charge_state_raw", "shadow_pcm_vVehicleFilt", "push_velocity_raw")
REPLACED_0_4_0 = (
    "ride_mode_raw",
    "power_state_raw",
    "speed_raw",
    "shadow_hu_NModeSelectorKnob",
    "shadow_bms_NState",
)


def _pre_create(
    registry: er.EntityRegistry,
    entry: MockConfigEntry,
    key: str,
    *,
    domain: str = "sensor",
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> str:
    return registry.async_get_or_create(
        domain, DOMAIN, f"{VEHICLE_ID}_{key}", config_entry=entry, disabled_by=disabled_by
    ).entity_id


async def test_setup_removes_exactly_the_merged_away_entities(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    # Another (not loaded) config entry of the same domain: its registry entries are not ours.
    other = make_entry(unique_id="someone-else", disabled_by=ConfigEntryDisabler.USER)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    user = er.RegistryEntryDisabler.USER
    # Replaced entities are orphans: removed even when the user disabled them.
    removed = [
        _pre_create(registry, entry, key, disabled_by=user if key == "power_state_raw" else None)
        for key in (*REMOVED_0_3_0, *REPLACED_0_4_0)
    ]
    kept = [
        _pre_create(registry, entry, "shadow_pcm_vVehicleFiltX"),
        _pre_create(registry, entry, "push_battery_charging_state_raw"),
        _pre_create(registry, entry, "charge_state"),
        _pre_create(registry, entry, "charge_state_raw", domain="binary_sensor"),
        # The stream's own (different) enums keep their discovered entities.
        _pre_create(registry, entry, "push_power_state_raw"),
        _pre_create(registry, entry, "push_ride_mode_raw"),
        _pre_create(registry, entry, "shadow_hu_NModeSelectorKnobX"),
        _pre_create(registry, entry, "ride_mode"),
        _pre_create(registry, entry, "speed"),
        _pre_create(registry, entry, "power_state_raw", domain="binary_sensor"),
    ]
    user_disabled = _pre_create(registry, entry, "shadow_bms_xNewThing", disabled_by=user)
    foreign = registry.async_get_or_create(
        "sensor", DOMAIN, f"{VEHICLE_ID_2}_charge_state_raw", config_entry=other
    ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in removed:
        assert registry.async_get(entity_id) is None, entity_id
        assert hass.states.get(entity_id) is None, entity_id
    for entity_id in [*kept, foreign, user_disabled]:
        assert registry.async_get(entity_id) is not None, entity_id
    assert registry.async_get(user_disabled).disabled_by is user  # untouched
    # Not re-created by the platform's restore loop for discovered sensors, nor by a later poll.
    await get_coordinator(entry).async_refresh()
    await hass.async_block_till_done()
    for key in (*REMOVED_0_3_0, *REPLACED_0_4_0):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_{key}") is None, key
    for key in ("speed", "ride_mode", "power_state", "knob_position", "bms_state"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_{key}"), key
    assert registry.async_get(user_disabled).disabled_by is user
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_re_enables_integration_disabled_entities_only(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    integration = er.RegistryEntryDisabler.INTEGRATION
    re_enabled = [
        _pre_create(registry, entry, "battery_level_real", disabled_by=integration),
        _pre_create(
            registry, entry, "push_connected", domain="binary_sensor", disabled_by=integration
        ),
        _pre_create(registry, entry, "shadow_bms_xNewThing", disabled_by=integration),
        _pre_create(registry, entry, "push_bms_state_raw", disabled_by=integration),
    ]
    by_user = _pre_create(
        registry, entry, "shadow_foo_bTest", disabled_by=er.RegistryEntryDisabler.USER
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in re_enabled:
        assert registry.async_get(entity_id).disabled_by is None, entity_id
    assert hass.states.get(re_enabled[0]).state == "91.25"  # added without a reload
    assert registry.async_get(by_user).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(by_user) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_keeps_entities_disabled_when_new_entities_are_not_enabled(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    entry = make_entry(pref_disable_new_entities=True)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    disabled = _pre_create(
        registry, entry, "battery_level_real", disabled_by=er.RegistryEntryDisabler.INTEGRATION
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(disabled).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------ stream connection -> pyalso
async def test_connection_changes_reach_the_vehicle_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_pyalso: PyalsoMocks
) -> None:
    coordinator = get_coordinator(init_integration)
    stream = mock_pyalso.streams.latest()
    assert not coordinator.data[VEHICLE_ID].stream_connected
    stream.status(True)
    assert coordinator.data[VEHICLE_ID].stream_connected
    stream.emit(battery_event(55, at=BASE_TIME + 100))
    assert coordinator.data[VEHICLE_ID].battery_level_merged.source == "stream"
    stream.status(False, "stream_ended")
    assert not coordinator.data[VEHICLE_ID].stream_connected
    assert coordinator.data[VEHICLE_ID].battery_level == 87.5  # REST at once

    stream.status(True)
    assert coordinator.data[VEHICLE_ID].stream_connected
    await coordinator.async_stop_push()  # stopping a stream marks it disconnected as well
    assert not coordinator.data[VEHICLE_ID].stream_connected
    assert not mock_pyalso.client.real.set_stream_connected(VEHICLE_ID, False).stream_connected
