"""Base entity and platform helpers for the ALSO integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyalso import VehicleState

from .const import DOMAIN, MANUFACTURER
from .coordinator import AlsoConfigEntry, AlsoCoordinator


class AlsoEntity(CoordinatorEntity[AlsoCoordinator]):
    """An entity belonging to one bike (one device per ``vehicle_reference_id``)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._attr_unique_id = f"{vehicle_id}_{key}"
        state = coordinator.data[vehicle_id]
        vehicle = state.vehicle
        # The stream reports the model name and firmware version; the coordinator keeps the
        # device registry current when they arrive later.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vehicle_id)},
            name=(vehicle.name if vehicle else None) or "ALSO e-bike",
            manufacturer=MANUFACTURER,
            model=state.model_name or (vehicle.vehicle_type if vehicle else None),
            sw_version=state.firmware_version,
            serial_number=vehicle.serial_number if vehicle else None,
        )

    @property
    def vehicle_state(self) -> VehicleState | None:
        """Latest state of this bike, or ``None`` if it is no longer on the account."""
        return (self.coordinator.data or {}).get(self._vehicle_id)

    @property
    def available(self) -> bool:
        """Available while EITHER source is healthy: the last REST poll worked, or the push
        stream is connected / delivered data recently (a failing poll must not blank live data)."""
        coordinator = self.coordinator
        healthy = coordinator.last_update_success or coordinator.has_recent_push(self._vehicle_id)
        return healthy and self.vehicle_state is not None


@callback
def async_add_vehicle_entities(
    entry: AlsoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[AlsoCoordinator, str], list[Entity]],
) -> None:
    """Add ``factory(coordinator, vehicle_id)`` entities now and for bikes that appear later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [vid for vid in coordinator.data if vid not in known]
        if not new:
            return
        known.update(new)
        async_add_entities([entity for vid in new for entity in factory(coordinator, vid)])

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
