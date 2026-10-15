"""Shared fixtures. pyalso is always mocked: no network is ever used."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pyalso import ConnectionStatus, StreamEvent, VehicleState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.coordinator import AlsoCoordinator

from .common import VEHICLE_ID, make_entry, make_state, make_vehicle


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let HA load custom_components/also in every test."""


class FakeStream:
    """Stands in for ``pyalso.AlsoStream``: tests push events and connection changes by hand."""

    def __init__(self, vehicle_id: str, start_error: Exception | None = None) -> None:
        self.vehicle_id = vehicle_id
        self.start_error = start_error
        self.start_calls = 0
        self.stop_calls = 0
        self.running = False
        self._events: list[Callable[[StreamEvent], None]] = []
        self._connections: list[Callable[[ConnectionStatus], None]] = []

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    def add_event_listener(self, callback: Callable[[StreamEvent], None]) -> Callable[[], None]:
        self._events.append(callback)
        return lambda: self._events.remove(callback) if callback in self._events else None

    def add_connection_listener(
        self, callback: Callable[[ConnectionStatus], None]
    ) -> Callable[[], None]:
        self._connections.append(callback)
        return lambda: self._connections.remove(callback) if callback in self._connections else None

    @property
    def listener_count(self) -> int:
        return len(self._events) + len(self._connections)

    # ---- test helpers
    def emit(self, event: StreamEvent) -> None:
        for callback in list(self._events):
            callback(event)

    def status(self, connected: bool, reason: str = "connected", **kwargs: Any) -> None:
        for callback in list(self._connections):
            callback(ConnectionStatus(connected, reason, **kwargs))


@dataclass
class FakeStreams:
    """Every stream the integration created, in order."""

    created: list[FakeStream] = field(default_factory=list)
    start_error: Exception | None = None

    def create(self, vehicle_id: str) -> FakeStream:
        stream = FakeStream(vehicle_id, self.start_error)
        self.created.append(stream)
        return stream

    def latest(self, vehicle_id: str = VEHICLE_ID) -> FakeStream:
        return next(s for s in reversed(self.created) if s.vehicle_id == vehicle_id)

    @property
    def running(self) -> list[FakeStream]:
        return [s for s in self.created if s.running]


@dataclass
class PyalsoMocks:
    """Handles on the patched pyalso classes used by the integration setup."""

    auth_cls: MagicMock
    client_cls: MagicMock
    client: MagicMock
    streams: FakeStreams
    #: What the fake REST API returns for a bike; replace to change the polled data.
    rest_state: Callable[[str], VehicleState] = make_state


@pytest.fixture
def mock_pyalso() -> Generator[PyalsoMocks]:
    """Patch AlsoAuth/AlsoClient/AlsoStream as imported by ``custom_components.also``.

    The fake client keeps a per-bike state cache and merges pushed updates with the REAL pyalso
    ``VehicleState`` logic (newest source time wins), exactly as ``AlsoClient`` does.
    """
    states: dict[str, VehicleState] = {}
    streams = FakeStreams()
    client = MagicMock()
    client.list_all_vehicles = AsyncMock(return_value=[make_vehicle()])
    mocks = PyalsoMocks(MagicMock(), MagicMock(), client, streams)

    def _get_vehicle_state(vid: str, vehicle: object = None) -> VehicleState:
        state = mocks.rest_state(vid)
        stamped = state.with_shadow_times(float(state.shadow.get("con_tUtc", 0)) or None)
        previous = states.get(vid)
        if previous is not None:
            stamped = stamped.carry_newer_from(previous)
        states[vid] = stamped
        return stamped

    def _apply_push_update(
        vid: str,
        partial: dict[str, Any],
        *,
        push: dict[str, Any] | None = None,
        updated_at: float | None = None,
    ) -> VehicleState:
        base = states.get(vid) or VehicleState(vehicle_id=vid)
        state = base.merge_update(partial, push, updated_at=updated_at)
        states[vid] = state
        return state

    client.get_vehicle_state = AsyncMock(side_effect=_get_vehicle_state)
    client.apply_push_update = MagicMock(side_effect=_apply_push_update)
    with (
        patch("custom_components.also.AlsoAuth", mocks.auth_cls),
        patch("custom_components.also.AlsoClient", return_value=client) as client_cls,
        patch(
            "custom_components.also.AlsoStream",
            side_effect=lambda auth, vehicle_id: streams.create(vehicle_id),
        ),
    ):
        mocks.client_cls = client_cls
        yield mocks


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> AsyncGenerator[MockConfigEntry]:
    """A set-up config entry with one fake bike."""
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def get_coordinator(entry: MockConfigEntry) -> AlsoCoordinator:
    return entry.runtime_data
