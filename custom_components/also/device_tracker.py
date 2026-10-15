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
        # The altitude unit is NOT verified (it is not metres), so it is passed on as reported.
        return {"altitude_raw": state.altitude_raw}
