"""Device tracker for the ALSO integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AlsoConfigEntry, AlsoCoordinator
from .entity import AlsoEntity, async_add_vehicle_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: AlsoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the GPS tracker."""
    async_add_vehicle_entities(
        entry, async_add_entities, lambda coordinator, vid: [AlsoTracker(coordinator, vid)]
    )


class AlsoTracker(AlsoEntity, TrackerEntity):
    """Last known GPS position of the bike (degrees)."""

    _attr_translation_key = "location"
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str) -> None:
        super().__init__(coordinator, vehicle_id, "location")

    # ``gps_accuracy`` is deliberately NOT overridden (Home Assistant's default of 0 m applies).
    # pyalso exposes the cloud's accuracy value (``location.accuracy_raw`` and the stream's
    # ``gps_accuracy_raw``) but its unit is only "metres" according to the public schema and has
    # never been verified against a known fix. Feeding an unverified number into zone enter/leave
    # logic could be worse than the default, so it is not used until the unit is confirmed.
    @property
    def latitude(self) -> float | None:
        state = self.vehicle_state
        return state.latitude if state else None

    @property
    def longitude(self) -> float | None:
        state = self.vehicle_state
        return state.longitude if state else None

    @property
    def available(self) -> bool:
        return super().available and self.latitude is not None and self.longitude is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.vehicle_state
        if state is None or state.altitude_raw is None:
            return None
        # The altitude unit is NOT verified (probably millimetres, an inference), so it is passed
        # on exactly as reported.
        return {"altitude_raw": state.altitude_raw}
