"""Contract tests: the REAL pyalso ``AlsoAuth`` and ``AlsoClient`` against a local fake service.

The rest of the suite mocks pyalso's network layer, which is fast but cannot show that the
integration and the library agree on the two riskiest seams. These tests run pyalso's own code over
real HTTP (127.0.0.1 only):

* token rotation through the Home Assistant adapter (``_token_persister``), including a failing
  persistence write and what a restart does afterwards;
* the race between a REST poll and a pushed update on a multi-bike account.

Only the push STREAM is faked (``FakeStream``): its gRPC transport is covered by pyalso's tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pyalso import AlsoAuth, AlsoClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .common import (
    ACCESS_TOKEN,
    BASE_TIME,
    REFRESH_TOKEN,
    VEHICLE_ID,
    VEHICLE_ID_2,
    battery_event,
    make_entry,
)
from .conftest import FakeStreams, get_coordinator
from .fake_service import FakeAlsoService

ISSUE_TOKEN_STORE = "token_store_failed"  # the repair issue raised when a write fails


@dataclass
class Wired:
    """Handles on what ``async_setup_entry`` built with the REAL pyalso classes."""

    streams: FakeStreams
    auths: list[AlsoAuth]


@pytest.fixture
async def service(socket_enabled: None) -> AsyncGenerator[FakeAlsoService]:
    fake = FakeAlsoService()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def two_bike_service(socket_enabled: None) -> AsyncGenerator[FakeAlsoService]:
    fake = FakeAlsoService((VEHICLE_ID, VEHICLE_ID_2))
    await fake.start()
    yield fake
    await fake.stop()


def _wire(fake: FakeAlsoService) -> Generator[Wired]:
    streams = FakeStreams()
    auths: list[AlsoAuth] = []

    def make_auth(session: object, **kwargs: object) -> AlsoAuth:
        auth = AlsoAuth(session, base_url=fake.base_url, **kwargs)  # type: ignore[arg-type]
        auths.append(auth)
        return auth

    def make_client(session: object, auth: AlsoAuth) -> AlsoClient:
        return AlsoClient(session, auth, base_url=fake.base_url)  # type: ignore[arg-type]

    with (
        patch("custom_components.also.AlsoAuth", side_effect=make_auth),
        patch("custom_components.also.AlsoClient", side_effect=make_client),
        patch(
            "custom_components.also.AlsoStream",
            side_effect=lambda auth, vehicle_id: streams.create(vehicle_id),
        ),
    ):
        yield Wired(streams, auths)


@pytest.fixture
def wired(service: FakeAlsoService) -> Generator[Wired]:
    yield from _wire(service)


@pytest.fixture
def wired_two(two_bike_service: FakeAlsoService) -> Generator[Wired]:
    yield from _wire(two_bike_service)


def _expired_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry whose access token is long expired, so setup performs a real rotation."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_EXPIRES_AT: 1.0})
    return entry


def _stored_entry_data(hass_storage: dict[str, dict], entry: MockConfigEntry) -> dict:
    stored = hass_storage["core.config_entries"]["data"]["entries"]
    return next(e["data"] for e in stored if e["entry_id"] == entry.entry_id)


async def _flush_storage(hass: HomeAssistant) -> None:
    """Let Home Assistant's delayed config-entry write run.

    The write is debounced on the real loop clock (config entries wait ``SAVE_DELAY`` = 1 s after
    the LAST change), so this really waits: it is also the window a crash would lose.
    """
    await asyncio.sleep(1.2)
    await hass.async_block_till_done()


# ------------------------------------------------------------------------------ rotation
async def test_rotation_is_used_persisted_and_survives_a_restart(
    hass: HomeAssistant,
    service: FakeAlsoService,
    wired: Wired,
    hass_storage: dict[str, dict],
) -> None:
    entry = _expired_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The stored (spent) refresh token went to the server exactly once ...
    assert [b["refresh_token"] for b in service.refresh_bodies] == [REFRESH_TOKEN]
    # ... the rotated access token (never the stored one) authorised every REST call ...
    assert service.bearers and set(service.bearers) == {"Bearer rotated-access-1"}
    # ... and the rotated pair reached the config entry before that, then the disk.
    assert entry.data[CONF_ACCESS_TOKEN] == "rotated-access-1"
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh-1"
    assert "core.config_entries" not in hass_storage  # in memory only: the write is delayed
    await _flush_storage(hass)
    assert _stored_entry_data(hass_storage, entry)[CONF_REFRESH_TOKEN] == "rotated-refresh-1"

    # "Restart": reload from the stored data. The server accepts only the NEWEST refresh token,
    # so success proves the stored one is the live one.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert service.refresh_rejections == 0
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []

    # A second rotation (forced on the live auth object) is persisted as well.
    await wired.auths[-1].refresh()
    assert entry.data[CONF_REFRESH_TOKEN] == f"rotated-refresh-{service.rotations}"
    assert service.rotations == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failing_persistence_write_keeps_the_session_alive_and_is_visible(
    hass: HomeAssistant,
    service: FakeAlsoService,
    wired: Wired,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _expired_entry(hass)
    real_update = hass.config_entries.async_update_entry
    failures: list[str] = []

    def failing_update(config_entry: MockConfigEntry, **kwargs: object) -> bool:
        data = kwargs.get("data")
        if isinstance(data, dict) and data.get(CONF_REFRESH_TOKEN) == "rotated-refresh-1":
            failures.append("write")
            raise OSError("disk is full")
        return real_update(config_entry, **kwargs)  # type: ignore[arg-type]

    with patch.object(hass.config_entries, "async_update_entry", side_effect=failing_update):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The write failed, but the session works: the NEW access token is in memory and in use.
    assert failures == ["write"]
    assert entry.state is ConfigEntryState.LOADED
    assert set(service.bearers) == {"Bearer rotated-access-1"}
    assert entry.data[CONF_REFRESH_TOKEN] == REFRESH_TOKEN  # the entry still holds the spent one
    # It is reported distinctly: a log line (no token values) and a repair issue.
    assert "Could not store the rotated ALSO sign-in tokens (OSError)" in caplog.text
    assert "rotated-refresh-1" not in caplog.text and "disk is full" not in caplog.text
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_TOKEN_STORE) is not None

    # The next rotation (using the in-memory refresh token) is stored and clears the issue.
    await wired.auths[0].refresh()
    assert service.rotations == 2 and service.refresh_rejections == 0
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh-2"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_TOKEN_STORE) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_after_a_lost_write_asks_for_a_new_sign_in(
    hass: HomeAssistant, service: FakeAlsoService, wired: Wired
) -> None:
    """The true consequence of a failed write plus a restart, stated as a test."""
    entry = _expired_entry(hass)
    real_update = hass.config_entries.async_update_entry

    def lose_token_write(config_entry: MockConfigEntry, **kwargs: object) -> bool:
        if isinstance(kwargs.get("data"), dict) and "rotated" in str(kwargs["data"]):
            raise OSError("write lost")
        return real_update(config_entry, **kwargs)  # type: ignore[arg-type]

    with patch.object(hass.config_entries, "async_update_entry", side_effect=lose_token_write):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)

    # Restart: the entry still holds the refresh token the server already spent.
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert service.refresh_rejections == 1
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


# ------------------------------------------------------------------- REST / push race
async def test_a_push_that_lands_mid_poll_survives_on_a_two_bike_account(
    hass: HomeAssistant, two_bike_service: FakeAlsoService, wired_two: Wired
) -> None:
    """Bike one's REST result is ready while bike two's calls are still in flight; a push for
    bike one arrives in between and must not be replaced by the older REST reading."""
    service = two_bike_service
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    level_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID}_battery_level")
    assert level_id and float(hass.states.get(level_id).state) == 87.5  # REST value
    coordinator = get_coordinator(entry)

    service.hold_shadow(VEHICLE_ID_2)
    poll = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(service.gate_reached[VEHICLE_ID_2].wait(), 5)
    # Make sure bike one's three REST calls (setup + this poll = 2 answers each) are complete and
    # processed by the client before the push arrives.
    await asyncio.wait_for(service.wait_answered(VEHICLE_ID, "connectivity", 2), 5)
    await asyncio.wait_for(service.wait_answered(VEHICLE_ID, "location", 2), 5)
    await asyncio.sleep(0.1)
    # Bike one's REST result is ready; now its stream delivers a newer battery level.
    wired_two.streams.latest(VEHICLE_ID).emit(battery_event(23, at=BASE_TIME + 300))
    assert float(hass.states.get(level_id).state) == 23
    service.release_shadow(VEHICLE_ID_2)
    await asyncio.wait_for(poll, 5)
    await hass.async_block_till_done()

    assert float(hass.states.get(level_id).state) == 23  # not rolled back to the older 87.5
    assert coordinator.data[VEHICLE_ID].battery_level == 23
    other = registry.async_get_entity_id("sensor", DOMAIN, f"{VEHICLE_ID_2}_battery_level")
    assert other and float(hass.states.get(other).state) == 87.5
    await hass.config_entries.async_unload(entry.entry_id)


async def test_rest_and_push_agree_on_newest_wins_through_the_real_client(
    hass: HomeAssistant, service: FakeAlsoService, wired: Wired
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = get_coordinator(entry)
    stream = wired.streams.latest()
    stream.emit(battery_event(40, at=BASE_TIME + 200))
    assert coordinator.data[VEHICLE_ID].battery_level == 40
    # A REST poll whose report is OLDER than the pushed level does not roll it back ...
    await coordinator.async_refresh()
    assert coordinator.data[VEHICLE_ID].battery_level == 40
    # ... a REST report that is newer does.
    service.shadow_overrides[VEHICLE_ID] = {"bms_rSOCDisp": 70.0, "con_tUtc": BASE_TIME + 500}
    await coordinator.async_refresh()
    assert coordinator.data[VEHICLE_ID].battery_level == 70.0
    # The stream's own copy was kept apart from the REST shadow key the whole time.
    assert coordinator.data[VEHICLE_ID].push["battery_level_percent"] == 40
    assert coordinator.data[VEHICLE_ID].shadow["bms_rSOCDisp"] == 70.0
    await hass.config_entries.async_unload(entry.entry_id)


def test_the_entry_fixture_tokens_are_the_ones_the_fake_service_knows() -> None:
    assert FakeAlsoService().current_access == ACCESS_TOKEN
    assert FakeAlsoService().current_refresh == REFRESH_TOKEN
