"""Data update coordinator for the ALSO integration.

Two sources feed the same per-bike :class:`~pyalso.VehicleState`:

* the live push stream (one per bike, started after the first successful REST refresh): every
  decoded event is merged immediately, without a REST call;
* the REST poll, now the slower companion that also fills in shadow-only fields.

pyalso does the merging: every value carries the time its source produced it and only newer (or
equal) data replaces it, so a stale cached snapshot from the stream can never overwrite fresher
REST data, and a REST refresh never throws away newer pushed data.

If the stream cannot be used (``grpcio`` missing, repeated failures) the integration keeps working
on polling alone and logs ONE warning per outage (per bike), not one per retry.

Availability follows EITHER source: entities stay available while a REST poll fails as long as the
push stream DELIVERED DATA recently (see :meth:`AlsoCoordinator.has_recent_push`). A stream that is
only connected (keep-alive replies, no data) does not keep entities available, and a per-bike
timer re-evaluates availability at the freshness deadline, so entities flip to unavailable on time
even when nothing else happens. Home Assistant itself logs a failing poll once, when it starts
failing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pyalso import (
    AlsoApiError,
    AlsoAuthError,
    AlsoClient,
    AlsoConnectionError,
    AlsoPushUnavailableError,
    AlsoRateLimitError,
    ConnectionStatus,
    KeepAliveEvent,
    StreamEvent,
    UnknownPacket,
    UnsupportedReplyEvent,
    Vehicle,
    VehicleState,
    updates_from_event,
)

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PUSH_EXPIRY_GRACE_SECONDS,
    PUSH_FRESH_SECONDS,
    PUSH_WARN_AFTER_FAILURES,
    UNKNOWN_PACKET_HISTORY,
    VEHICLE_LIST_EVERY_N_UPDATES,
)
from .unknown_packets import summarize_event

_LOGGER = logging.getLogger(__name__)

type AlsoConfigEntry = ConfigEntry[AlsoCoordinator]


class PushStream(Protocol):
    """What the coordinator needs from a push transport (``pyalso.AlsoStream`` provides it)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def add_event_listener(self, callback: Callable[[StreamEvent], None]) -> Callable[[], None]: ...

    def add_connection_listener(
        self, callback: Callable[[ConnectionStatus], None]
    ) -> Callable[[], None]: ...


StreamFactory = Callable[[str], PushStream]


@dataclass
class PushStatus:
    """Connection state of one bike's push stream (shown by the diagnostic sensor)."""

    connected: bool = False
    #: Short machine tag of the last connection change (``connected``, ``stream_ended``, ...).
    reason: str | None = None
    #: When the last data event arrived (keep-alive replies do not count).
    last_event: datetime | None = None
    #: Failed connection attempts since the stream was last connected.
    failures: int = 0
    #: True when push cannot be used at all (grpc missing, stream gave up): polling only.
    unavailable: bool = False


@dataclass
class _StreamHandle:
    stream: PushStream
    unsubscribe: list[Callable[[], None]] = field(default_factory=list)


class AlsoCoordinator(DataUpdateCoordinator[dict[str, VehicleState]]):
    """Fetch the state of every vehicle on the account; data is keyed by vehicle id."""

    config_entry: AlsoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AlsoConfigEntry,
        client: AlsoClient,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        # Snapshot used by the update listener to reload only when options really changed.
        self.applied_options: dict[str, Any] = dict(entry.options)
        self._vehicles: dict[str, Vehicle] = {}
        self._updates = 0
        self._stream_factory = stream_factory
        self._streams: dict[str, _StreamHandle] = {}
        self._push_started = False
        self._push_disabled = False
        #: Bikes whose stream gave up for good (fatal status): not restarted by later polls.
        self._push_given_up: set[str] = set()
        #: Per-bike timers that re-evaluate availability when the last data event goes stale.
        self._expiry_timers: dict[str, CALLBACK_TYPE] = {}
        #: Bikes whose current push outage was already logged (re-armed per bike on reconnect).
        self._outage_logged: set[str] = set()
        self._push_disabled_logged = False
        self._push_status: dict[str, PushStatus] = {}
        self._device_info_sent: dict[str, tuple[str | None, str | None]] = {}
        #: Rolling window of redacted summaries of DISTINCT unknown packets/fields; a repeat of the
        #: same (kind, feature, command, status, field numbers) bumps ``count`` instead of adding.
        self._unknown_window: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        #: How many unknown packets/fields were seen in total (the window only keeps the last).
        self.unknown_packet_count = 0

    @property
    def unknown_packets(self) -> list[dict[str, Any]]:
        """Redacted summaries of the last distinct unknown packets/fields, newest last."""
        return list(self._unknown_window.values())

    def _remember_unknown(self, summary: dict[str, Any]) -> None:
        """Add ``summary`` to the window; repeats are collapsed so one cannot flood it."""
        key = (
            summary["kind"],
            summary["feature"],
            summary["command"],
            summary["status"],
            tuple(summary.get("field_numbers", ())),
            summary.get("reason"),
        )
        seen = self._unknown_window.get(key)
        if seen is None:
            summary["count"] = 1
            self._unknown_window[key] = summary
            while len(self._unknown_window) > UNKNOWN_PACKET_HISTORY:
                self._unknown_window.popitem(last=False)
            return
        seen["count"] += 1
        seen["last_timestamp"] = summary["timestamp"]
        self._unknown_window.move_to_end(key)

    @property
    def vehicles(self) -> dict[str, Vehicle]:
        """The cached vehicles by id."""
        return self._vehicles

    @property
    def push_enabled(self) -> bool:
        """True while push is in use for at least one bike; False once it was given up for the
        whole entry (grpc missing, start failure) or for every known bike (fatal stream status)."""
        if self._stream_factory is None or self._push_disabled:
            return False
        known = set(self.data or {})
        return not (known and known <= self._push_given_up)

    def stream_active(self, vehicle_id: str) -> bool:
        """True while a stream object exists for this bike (it may still be reconnecting)."""
        return vehicle_id in self._streams

    def has_recent_push(self, vehicle_id: str) -> bool:
        """True if the push stream of this bike DELIVERED DATA recently.

        Used for entity availability: live data keeps the entities available while a REST poll
        fails. "Recently" is three push cadences plus a margin (``PUSH_FRESH_SECONDS``) since the
        last data event; keep-alive replies are not data, and neither is a mere ``connected``
        flag (a stream can stay connected for hours without delivering anything). Time passing
        triggers nothing by itself: see :meth:`_arm_expiry`.
        """
        status = self._push_status.get(vehicle_id)
        if status is None or status.last_event is None:
            return False
        return (dt_util.utcnow() - status.last_event).total_seconds() <= PUSH_FRESH_SECONDS

    @callback
    def _arm_expiry(self, vehicle_id: str) -> None:
        """(Re)schedule the timer that rewrites availability when this bike's data goes stale."""
        self._cancel_expiry(vehicle_id)
        self._expiry_timers[vehicle_id] = async_call_later(
            self.hass,
            PUSH_FRESH_SECONDS + PUSH_EXPIRY_GRACE_SECONDS,
            partial(self._on_expiry, vehicle_id),
        )

    @callback
    def _cancel_expiry(self, vehicle_id: str) -> None:
        if (cancel := self._expiry_timers.pop(vehicle_id, None)) is not None:
            cancel()

    @callback
    def _on_expiry(self, vehicle_id: str, _now: datetime) -> None:
        """The freshness deadline passed: entities re-evaluate ``has_recent_push``."""
        self._expiry_timers.pop(vehicle_id, None)
        self.async_update_listeners()

    def push_status(self, vehicle_id: str) -> PushStatus:
        """Push state of one bike (a default, disconnected status for unknown bikes)."""
        status = self._push_status.get(vehicle_id)
        if status is None:
            status = self._push_status[vehicle_id] = PushStatus(unavailable=not self.push_enabled)
        return status

    # ------------------------------------------------------------------ REST polling
    async def _async_refresh_vehicles(self) -> None:
        listed = await self.client.list_all_vehicles()
        self._vehicles = {v.vehicle_reference_id: v for v in listed if v.vehicle_reference_id}

    async def _async_update_data(self) -> dict[str, VehicleState]:
        try:
            if not self._vehicles or self._updates % VEHICLE_LIST_EVERY_N_UPDATES == 0:
                await self._async_refresh_vehicles()
            self._updates += 1
            fetched = await asyncio.gather(
                *(self.client.get_vehicle_state(vid, v) for vid, v in self._vehicles.items())
            )
        except AlsoAuthError as err:
            # Includes AlsoSessionExpiredError and AlsoInvalidCodeError: log in again.
            raise ConfigEntryAuthFailed(str(err)) from err
        except AlsoRateLimitError as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except (AlsoConnectionError, AlsoApiError) as err:
            raise UpdateFailed(f"Error communicating with ALSO: {err}") from err
        # Each state was captured when ITS REST calls finished. A push that arrived since (while
        # another bike's calls were still in flight) lives in ``self.data``: fold it back in, newest
        # source time wins, so assigning the result cannot throw a newer pushed value away.
        previous = self.data or {}
        data: dict[str, VehicleState] = {}
        for state in fetched:
            earlier = previous.get(state.vehicle_id)
            folded = state.carry_newer_from(earlier) if earlier is not None else state
            # Keep the library's own object when nothing newer was folded in, so the identity check
            # in async_apply_push_update ("nothing changed") keeps working.
            data[state.vehicle_id] = state if folded == state else folded
        for vehicle_id, state in data.items():
            self._async_sync_device(vehicle_id, state)
        await self._async_sync_streams(set(data))
        return data

    # --------------------------------------------------------------------- push path
    @callback
    def async_apply_push_update(
        self,
        vehicle_id: str,
        partial_state: Mapping[str, Any],
        *,
        push: Mapping[str, Any] | None = None,
        updated_at: float | None = None,
    ) -> bool:
        """Merge a pushed update into one bike's state and notify the entities.

        ``partial_state`` holds shadow keys, ``push`` the stream-only values and ``updated_at`` is
        the unix time the data was produced at (pyalso keeps the newest value per key). Returns
        whether anything changed. The refresh timer is NOT reset: the REST poll keeps its own
        rhythm however busy the stream is.
        """
        current = (self.data or {}).get(vehicle_id)
        if current is None:
            _LOGGER.debug("Ignoring push update for an unknown vehicle")
            return False
        state = self.client.apply_push_update(
            vehicle_id, partial_state, push=push, updated_at=updated_at
        )
        if state is current:
            return False
        self.data = {**self.data, vehicle_id: state}
        self._async_sync_device(vehicle_id, state)
        self.async_update_listeners()
        return True

    @callback
    def _on_event(self, vehicle_id: str, event: StreamEvent) -> None:
        """Handle one decoded stream event."""
        if isinstance(event, KeepAliveEvent | UnsupportedReplyEvent):
            return
        summary = summarize_event(event)
        if summary is not None:
            self.unknown_packet_count += 1
            self._remember_unknown(summary)
            _LOGGER.debug(
                "Unknown stream data %s/%s status %s (%s bytes)",
                summary["feature"],
                summary["command"],
                summary["status"],
                summary["length"],
            )
        self.push_status(vehicle_id).last_event = dt_util.utcnow()
        self._arm_expiry(vehicle_id)
        changed = False
        if not isinstance(event, UnknownPacket):
            update = updates_from_event(event)
            if update is not None:
                changed = self.async_apply_push_update(
                    vehicle_id, update.shadow, push=update.push, updated_at=update.updated_at
                )
        if not changed:
            self.async_update_listeners()  # the "last push" attribute moved

    @callback
    def _on_connection(self, vehicle_id: str, status: ConnectionStatus) -> None:
        """Handle a connection change reported by one stream."""
        push_status = self.push_status(vehicle_id)
        push_status.connected = status.connected
        push_status.reason = status.reason
        if status.connected:
            push_status.failures = 0
            push_status.unavailable = False
            if vehicle_id in self._outage_logged:
                _LOGGER.info("Live updates from ALSO are back")
                self._outage_logged.discard(vehicle_id)
        else:
            push_status.failures += 1
            if status.fatal:
                self._on_fatal(vehicle_id, status)
            elif (
                push_status.failures >= PUSH_WARN_AFTER_FAILURES
                and vehicle_id not in self._outage_logged
            ):
                self._outage_logged.add(vehicle_id)
                _LOGGER.warning(
                    "Live updates from ALSO are unavailable (last reason: %s). "
                    "Falling back to polling every %s s; the connection keeps retrying",
                    status.reason,
                    int(self.update_interval.total_seconds()) if self.update_interval else 0,
                )
        self.async_update_listeners()

    @callback
    def _on_fatal(self, vehicle_id: str, status: ConnectionStatus) -> None:
        """A stream gave up for good: re-authenticate, or fall back to polling only.

        The stream object is released (stopped, listeners detached) and the bike is marked as
        given up, so later polls do not restart it and diagnostics report polling only. Cleanup
        does not depend on how pyalso tore itself down.
        """
        self.push_status(vehicle_id).unavailable = True
        self._push_given_up.add(vehicle_id)
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_stop_stream(vehicle_id, keep_reason=True),
            name="also_release_failed_stream",
        )
        if isinstance(status.error, AlsoAuthError):
            # Same outcome as ConfigEntryAuthFailed raised by a refresh: start the reauth flow.
            _LOGGER.debug("Stream authentication failed; starting reauthentication")
            self.config_entry.async_start_reauth(self.hass)
        elif vehicle_id not in self._outage_logged:
            self._outage_logged.add(vehicle_id)
            _LOGGER.warning(
                "Live updates from ALSO stopped (%s). Falling back to polling", status.reason
            )

    # ---------------------------------------------------------------- stream lifecycle
    async def async_start_push(self) -> None:
        """Start one stream per bike. Call after the first successful refresh."""
        if self._stream_factory is None or self._push_disabled:
            return
        self._push_started = True
        await self._async_sync_streams(set(self.data or {}))

    async def _async_sync_streams(self, wanted: set[str]) -> None:
        """Start streams for new bikes and stop those of bikes that left the account."""
        if not self._push_started:
            return
        for vehicle_id in [v for v in self._streams if v not in wanted]:
            await self._async_stop_stream(vehicle_id)
        for vehicle_id in sorted(wanted - set(self._streams) - self._push_given_up):
            if self._push_disabled:
                return
            await self._async_start_stream(vehicle_id)

    async def _async_start_stream(self, vehicle_id: str) -> None:
        assert self._stream_factory is not None
        stream = self._stream_factory(vehicle_id)
        handle = _StreamHandle(
            stream,
            [
                stream.add_event_listener(partial(self._on_event, vehicle_id)),
                stream.add_connection_listener(partial(self._on_connection, vehicle_id)),
            ],
        )
        self.push_status(vehicle_id)
        try:
            await stream.start()
        except AlsoPushUnavailableError:
            self._unsubscribe(handle)
            await self._async_disable_push("the grpcio package is not installed")
            return
        except Exception as err:  # noqa: BLE001 - never let push break the polling setup
            self._unsubscribe(handle)
            await self._async_disable_push(f"the stream could not start ({type(err).__name__})")
            return
        self._streams[vehicle_id] = handle

    @staticmethod
    def _unsubscribe(handle: _StreamHandle) -> None:
        for unsubscribe in handle.unsubscribe:
            unsubscribe()
        handle.unsubscribe.clear()

    async def _async_stop_stream(self, vehicle_id: str, *, keep_reason: bool = False) -> None:
        """Stop and forget one bike's stream; ``keep_reason`` keeps the last status reason (the
        fatal one) instead of replacing it with ``stopped``."""
        handle = self._streams.pop(vehicle_id, None)
        if handle is None:
            return
        self._unsubscribe(handle)
        try:
            await handle.stream.stop()
        except Exception as err:  # noqa: BLE001 - shutting down; nothing to recover
            _LOGGER.debug("Stopping a stream failed (%s)", type(err).__name__)
        status = self._push_status.get(vehicle_id)
        if status is not None:
            status.connected = False
            if not keep_reason:
                status.reason = "stopped"
        if keep_reason:
            self.async_update_listeners()  # a fatal stop changes what entities and diagnostics show

    async def _async_disable_push(self, why: str) -> None:
        """Give up on push for this entry: warn ONCE and keep polling."""
        self._push_disabled = True
        for status in self._push_status.values():
            status.unavailable = True
            status.connected = False
        if not self._push_disabled_logged:
            self._push_disabled_logged = True
            _LOGGER.warning(
                "Live updates from ALSO are unavailable because %s. "
                "Falling back to polling every %s s",
                why,
                int(self.update_interval.total_seconds()) if self.update_interval else 0,
            )
        await self.async_stop_push()
        self.async_update_listeners()

    async def async_stop_push(self) -> None:
        """Stop every stream and wait until all their tasks are gone. Safe to call repeatedly."""
        self._push_started = False
        for vehicle_id in list(self._streams):
            await self._async_stop_stream(vehicle_id)
        for vehicle_id in list(self._expiry_timers):
            self._cancel_expiry(vehicle_id)

    async def async_shutdown(self) -> None:
        """Stop the streams along with the coordinator (entry unload)."""
        await super().async_shutdown()
        await self.async_stop_push()

    # ---------------------------------------------------------------- device registry
    @callback
    def _async_sync_device(self, vehicle_id: str, state: VehicleState) -> None:
        """Keep the device's model and software version current (the stream reports both)."""
        model = state.model_name or (state.vehicle.vehicle_type if state.vehicle else None)
        sw_version = state.firmware_version
        if self._device_info_sent.get(vehicle_id) == (model, sw_version):
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, vehicle_id)})
        if device is None:
            return  # entities are not set up yet: they build their device info from the state
        self._device_info_sent[vehicle_id] = (model, sw_version)
        registry.async_update_device(
            device.id,
            model=model if model is not None else UNDEFINED,
            sw_version=sw_version if sw_version is not None else UNDEFINED,
        )
