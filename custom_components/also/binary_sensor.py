"""Binary sensors for the ALSO integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AlsoConfigEntry, AlsoCoordinator
from .entity import AlsoEntity, async_add_vehicle_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: AlsoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the binary sensors."""
    async_add_vehicle_entities(
        entry,
        async_add_entities,
        lambda coordinator, vid: [
            AlsoConnectedSensor(coordinator, vid),
            AlsoPushConnectedSensor(coordinator, vid),
        ],
    )


class AlsoConnectedSensor(AlsoEntity, BinarySensorEntity):
    """Whether the bike's telematics unit is connected to the ALSO cloud."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connected"

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str) -> None:
        super().__init__(coordinator, vehicle_id, "connected")

    @property
    def is_on(self) -> bool | None:
        state = self.vehicle_state
        return state.connected if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.vehicle_state
        if state is None or state.connectivity is None:
            return None
        return {"disconnect_reason": state.connectivity.disconnect_reason}


class AlsoPushConnectedSensor(AlsoEntity, BinarySensorEntity):
    """Whether the live push stream of this bike is up (diagnostic)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "push_connected"

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str) -> None:
        super().__init__(coordinator, vehicle_id, "push_connected")

    @property
    def available(self) -> bool:
        # The stream state is independent of REST polling: report it even while a poll failed.
        return self.vehicle_state is not None

    @property
    def is_on(self) -> bool:
        return self.coordinator.push_status(self._vehicle_id).connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.push_status(self._vehicle_id)
        return {
            "last_push": status.last_event.isoformat() if status.last_event else None,
            "last_status": status.reason,
            "failed_attempts": status.failures,
            "polling_only": status.unavailable,
        }
