"""The ALSO integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyalso import AlsoAuth, AlsoClient, AlsoStream, Tokens

from .const import PLATFORMS
from .coordinator import AlsoConfigEntry, AlsoCoordinator, PushStream

_LOGGER = logging.getLogger(__name__)

# The odometer sensor used to be called ``odometer_raw`` (unit unverified); the unit is miles now.
_OLD_ODOMETER_SUFFIX = "_odometer_raw"
_NEW_ODOMETER_SUFFIX = "_odometer"


def _token_persister(
    hass: HomeAssistant, entry: AlsoConfigEntry
) -> Callable[[Tokens], Awaitable[None]]:
    """Build the callback pyalso awaits after EVERY login/refresh.

    The refresh token rotates, so the new set must reach the config entry before it is used.
    """

    async def _persist(tokens: Tokens) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **tokens.to_dict()})

    return _persist


@callback
def _migrate_odometer(entry: er.RegistryEntry) -> dict[str, str] | None:
    """Keep the history of the odometer sensor when its key changes."""
    if entry.domain == "sensor" and entry.unique_id.endswith(_OLD_ODOMETER_SUFFIX):
        stem = entry.unique_id.removesuffix(_OLD_ODOMETER_SUFFIX)
        return {"new_unique_id": f"{stem}{_NEW_ODOMETER_SUFFIX}"}
    return None


async def async_setup_entry(hass: HomeAssistant, entry: AlsoConfigEntry) -> bool:
    """Set up ALSO from a config entry."""
    try:
        tokens = Tokens.from_dict(entry.data)
    except (ValueError, TypeError) as err:
        raise ConfigEntryAuthFailed("Stored credentials are incomplete") from err
    session = async_get_clientsession(hass)
    auth = AlsoAuth(session, tokens=tokens, token_update_callback=_token_persister(hass, entry))

    def _create_stream(vehicle_id: str) -> PushStream:
        return AlsoStream(auth, vehicle_id)

    coordinator = AlsoCoordinator(hass, entry, AlsoClient(session, auth), _create_stream)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await er.async_migrate_entries(hass, entry.entry_id, _migrate_odometer)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    # Push starts after the first successful REST refresh and once the entities exist. The
    # coordinator stops it on unload (``async_shutdown``); this covers Home Assistant stopping.
    async def _stop_push(_event: Event) -> None:
        await coordinator.async_stop_push()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_push))
    await coordinator.async_start_push()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: AlsoConfigEntry) -> None:
    """Reload only when options changed; token rotation also updates the entry (data only)."""
    if dict(entry.options) != entry.runtime_data.applied_options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AlsoConfigEntry) -> bool:
    """Unload a config entry; the coordinator stops its streams when the entry unloads."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop_push()
    return unloaded
