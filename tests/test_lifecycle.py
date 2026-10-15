"""Stream lifecycle after failures, import preloading, device removal and poll/push races."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pyalso import AlsoAuthError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import DOMAIN
from custom_components.also.diagnostics import async_get_config_entry_diagnostics

from .common import (
    BASE_TIME,
    VEHICLE_ID,
    VEHICLE_ID_2,
    battery_event,
    make_entry,
    make_vehicle,
)
from .conftest import PyalsoMocks, get_coordinator


# ------------------------------------------------------------------ fatal stream status
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AlsoAuthError(401, None, "expired"), "auth_failed"),
        (RuntimeError("supervisor bug"), "internal_error"),
    ],
)
async def test_a_fatal_status_releases_the_stream_and_diagnostics_tell_the_truth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_pyalso: PyalsoMocks,
    error: Exception,
    reason: str,
) -> None:
    """Regression: the handle stayed registered with both listeners attached, ``stop()`` was never
    awaited and diagnostics kept saying push was enabled."""
    coordinator = get_coordinator(init_integration)
    stream = mock_pyalso.streams.latest()
    assert coordinator.push_enabled and coordinator.stream_active(VEHICLE_ID)

    stream.status(False, reason, error=error, fatal=True)
    await hass.async_block_till_done()

    assert stream.stop_calls == 1 and not stream.running
    assert stream.listener_count == 0  # both listeners detached
    assert not coordinator.stream_active(VEHICLE_ID)
    status = coordinator.push_status(VEHICLE_ID)
    assert status.unavailable and not status.connected and status.reason == reason
    assert coordinator.push_enabled is False  # the only bike gave up

    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["push_enabled"] is False
    assert diag["push"][0]["polling_only"] is True
    assert diag["push"][0]["stream_active"] is False
    assert diag["push"][0]["last_status"] == reason

    # Later polls do not resurrect it, and polling keeps working.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(mock_pyalso.streams.created) == 1
    assert coordinator.last_update_success


async def test_a_fatal_status_on_one_bike_leaves_the_other_stream_running(
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
    coordinator = get_coordinator(entry)

    mock_pyalso.streams.latest(VEHICLE_ID).status(
        False, "internal_error", error=RuntimeError("bug"), fatal=True
    )
    await hass.async_block_till_done()
    assert not coordinator.stream_active(VEHICLE_ID)
    assert coordinator.stream_active(VEHICLE_ID_2)
    assert mock_pyalso.streams.latest(VEHICLE_ID_2).running
    assert coordinator.push_enabled  # still in use for the other bike
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------------- grpc import
async def test_grpc_is_imported_in_an_executor_before_the_first_stream_starts(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    """Regression: ``AlsoStream.start`` imports grpcio with a plain ``import`` on the event loop."""
    loop_thread = threading.get_ident()
    events: list[tuple[str, int]] = []

    def preload() -> None:
        events.append(("preload", threading.get_ident()))

    original_create = mock_pyalso.streams.create

    def create(vehicle_id: str):  # type: ignore[no-untyped-def]
        stream = original_create(vehicle_id)
        original_start = stream.start

        async def start() -> None:
            events.append(("start", threading.get_ident()))
            await original_start()

        stream.start = start  # type: ignore[method-assign]
        return stream

    mock_pyalso.streams.create = create  # type: ignore[method-assign]
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch("custom_components.also._preload_grpc", preload):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert [name for name, _ in events] == ["preload", "start"]  # before the first stream
    assert events[0][1] != loop_thread  # and off the event loop
    await hass.config_entries.async_unload(entry.entry_id)


async def test_preload_tolerates_a_missing_grpc_package() -> None:
    import sys

    from custom_components.also import _preload_grpc

    with patch.dict(sys.modules, {"grpc": None, "grpc.aio": None}):
        _preload_grpc()  # no exception: AlsoStream.start() reports the missing package


# ------------------------------------------------------------------- device removal
async def test_devices_of_bikes_that_left_the_account_can_be_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    from custom_components.also import async_remove_config_entry_device

    devices = dr.async_get(hass)
    current = devices.async_get_device(identifiers={(DOMAIN, VEHICLE_ID)})
    assert current is not None
    # A bike that is still listed keeps its device ...
    assert not await async_remove_config_entry_device(hass, init_integration, current)
    # ... a device for a bike that is gone can be deleted.
    gone = devices.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "VHC-AAAA-BBBB-EEEE")}
    )
    assert await async_remove_config_entry_device(hass, init_integration, gone)
    # A foreign identifier never counts as one of ours.
    other = devices.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={("other", VEHICLE_ID)}
    )
    assert await async_remove_config_entry_device(hass, init_integration, other)


# --------------------------------------------------------------- poll / push race (mocked)
async def test_a_push_during_a_multi_bike_poll_is_not_replaced_by_the_older_rest_result(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    """Regression: the poll assigned the gather() results wholesale, throwing away a pushed
    value that arrived while another bike's calls were in flight."""
    mock_pyalso.client.list_all_vehicles.return_value = [
        make_vehicle(VEHICLE_ID),
        make_vehicle(VEHICLE_ID_2, "TESTSERIAL0000002"),
    ]
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = get_coordinator(entry)
    registry = er.async_get(hass)
    level = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_battery_level")
    assert level and float(hass.states.get(level).state) == 87.5

    reached = asyncio.Event()
    release = asyncio.Event()
    real_side_effect = mock_pyalso.client.get_vehicle_state.side_effect

    async def slow_for_bike_two(vid: str, vehicle: object = None):  # type: ignore[no-untyped-def]
        if vid == VEHICLE_ID_2:
            reached.set()
            await release.wait()
        return await real_side_effect(vid, vehicle)

    mock_pyalso.client.get_vehicle_state.side_effect = slow_for_bike_two
    poll = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(reached.wait(), 5)
    await asyncio.sleep(0.05)  # bike one's REST result is in, bike two's is still pending
    mock_pyalso.streams.latest(VEHICLE_ID).emit(battery_event(23, at=BASE_TIME + 300))
    assert float(hass.states.get(level).state) == 23
    release.set()
    await asyncio.wait_for(poll, 5)
    await hass.async_block_till_done()
    assert float(hass.states.get(level).state) == 23
    await hass.config_entries.async_unload(entry.entry_id)
