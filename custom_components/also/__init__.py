"""The ALSO integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyalso import AlsoAuth, AlsoClient, AlsoStream, Tokens

from .const import DOMAIN, PLATFORMS, REMOVED_ENTITY_KEYS
from .coordinator import AlsoConfigEntry, AlsoCoordinator, PushStream

_LOGGER = logging.getLogger(__name__)

# The odometer sensor used to be called ``odometer_raw`` (unit unverified); the unit is miles now.
_OLD_ODOMETER_SUFFIX = "_odometer_raw"
_NEW_ODOMETER_SUFFIX = "_odometer"

#: Repair issue shown while the latest rotated tokens could not be written to the config entry.
ISSUE_TOKEN_STORE = "token_store_failed"


def _token_persister(
    hass: HomeAssistant, entry: AlsoConfigEntry
) -> Callable[[Tokens], Awaitable[None]]:
    """Build the callback pyalso awaits after EVERY login/refresh.

    The refresh token rotates, so the new set must reach the config entry before it is used.

    What this does and does not guarantee (do not read more into it): the awaited call updates the
    config entry IN MEMORY before pyalso uses or returns the new access token, and asks Home
    Assistant to save it. Home Assistant writes config entries with a short delay (about one
    second) and once more when it stops, so the tokens are on disk shortly after, not at the
    moment this coroutine returns: a crash or power loss inside that window leaves the previous,
    already consumed refresh token on disk and the next start asks for a new sign-in. A failing
    update is not raised (pyalso keeps the new tokens in memory either way and the running
    integration keeps working): it is logged distinctly and a repair issue is shown until a later
    update succeeds. A later failure of the disk write itself is logged by Home Assistant's storage
    layer and cannot be reported back through this callback.
    """

    async def _persist(tokens: Tokens) -> None:
        try:
            hass.config_entries.async_update_entry(entry, data={**entry.data, **tokens.to_dict()})
        except Exception as err:  # noqa: BLE001 - never let a storage problem break the session
            _LOGGER.error(
                "Could not store the rotated ALSO sign-in tokens (%s). They are kept in memory; "
                "if Home Assistant restarts before a later update succeeds you must sign in again",
                type(err).__name__,
            )
            ir.async_create_issue(
                hass,
                DOMAIN,
                ISSUE_TOKEN_STORE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TOKEN_STORE,
            )
            return
        ir.async_delete_issue(hass, DOMAIN, ISSUE_TOKEN_STORE)

    return _persist


def _preload_grpc() -> None:
    """Import grpcio (a C extension) in an executor thread instead of on the event loop.

    ``AlsoStream.start`` imports it lazily with a plain ``import`` statement, which Home
    Assistant's blocking-import detector does not see. A missing package is not an error here:
    the stream reports it and the integration falls back to polling.
    """
    try:
        import grpc  # noqa: F401
        import grpc.aio  # noqa: F401
    except ImportError:
        pass


@callback
def _migrate_odometer(entry: er.RegistryEntry) -> dict[str, str] | None:
    """Keep the history of the odometer sensor when its key changes."""
    if entry.domain == "sensor" and entry.unique_id.endswith(_OLD_ODOMETER_SUFFIX):
        stem = entry.unique_id.removesuffix(_OLD_ODOMETER_SUFFIX)
        return {"new_unique_id": f"{stem}{_NEW_ODOMETER_SUFFIX}"}
    return None


def _is_removed_entity(reg: er.RegistryEntry) -> bool:
    """True for a sensor that 0.3.0 or 0.4.0 removed or replaced (see ``REMOVED_ENTITY_KEYS``).

    Unique ids are ``<vehicle_id>_<key>`` and vehicle ids contain no underscore, so the key is
    everything after the FIRST underscore and must match exactly.
    """
    if reg.domain != "sensor" or reg.platform != DOMAIN:
        return False
    vehicle_id, separator, key = reg.unique_id.partition("_")
    return bool(vehicle_id and separator) and key in REMOVED_ENTITY_KEYS


@callback
def _async_clean_up_registry(hass: HomeAssistant, entry: AlsoConfigEntry) -> None:
    """Delete the registry entries of removed entities and re-enable integration-disabled ones.

    The removed entities (0.3.0 merged values, 0.4.0 raw numbers replaced by named states) are
    orphans whatever their ``disabled_by``, so they are deleted even when the user disabled them.

    Before 0.3.0 some entities were disabled by default (``disabled_by=INTEGRATION``); every entity
    is enabled by default now, so those are re-enabled. Other entities disabled by the USER (or by a
    device, config entry or Home Assistant itself) are never touched, and nothing is re-enabled
    while the entry's "enable newly added entities" system option is off (Home Assistant marks new
    entities INTEGRATION-disabled then).
    """
    registry = er.async_get(hass)
    removed = enabled = 0
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if _is_removed_entity(reg):
            registry.async_remove(reg.entity_id)
            removed += 1
        elif (
            reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            and not entry.pref_disable_new_entities
        ):
            registry.async_update_entity(reg.entity_id, disabled_by=None)
            enabled += 1
    if removed or enabled:
        _LOGGER.debug("Removed %s obsolete and re-enabled %s entities", removed, enabled)


async def async_setup_entry(hass: HomeAssistant, entry: AlsoConfigEntry) -> bool:
    """Set up ALSO from a config entry."""
    try:
        tokens = Tokens.from_dict(entry.data)
    except (ValueError, TypeError) as err:
        raise ConfigEntryAuthFailed("Stored credentials are incomplete") from err
    ir.async_delete_issue(hass, DOMAIN, ISSUE_TOKEN_STORE)  # re-evaluated by this run
    session = async_get_clientsession(hass)
    auth = AlsoAuth(session, tokens=tokens, token_update_callback=_token_persister(hass, entry))

    def _create_stream(vehicle_id: str) -> PushStream:
        return AlsoStream(auth, vehicle_id)

    coordinator = AlsoCoordinator(hass, entry, AlsoClient(session, auth), _create_stream)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await er.async_migrate_entries(hass, entry.entry_id, _migrate_odometer)
    # Before the platforms: the sensor platform restores discovered sensors from the registry.
    _async_clean_up_registry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    # Push starts after the first successful REST refresh and once the entities exist. The
    # coordinator stops it on unload (``async_shutdown``); this covers Home Assistant stopping.
    async def _stop_push(_event: Event) -> None:
        await coordinator.async_stop_push()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_push))
    await hass.async_add_import_executor_job(_preload_grpc)  # before the first stream imports it
    await coordinator.async_start_push()
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: AlsoConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting the device of a bike that is no longer on the account.

    A bike that is still listed keeps its device: removing it would only bring it back at the
    next refresh.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return False
    current = set(coordinator.vehicles) | set(coordinator.data or {})
    return not any(
        domain == DOMAIN and identifier in current
        for domain, identifier in device_entry.identifiers
    )


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
