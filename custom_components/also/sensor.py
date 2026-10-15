"""Sensors for the ALSO integration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from pyalso import BatteryChargingState, VehicleState

from .const import EXPLICIT_SHADOW_KEYS, SHADOW_UNIQUE_ID_INFIX
from .coordinator import AlsoConfigEntry, AlsoCoordinator
from .entity import AlsoEntity
from .privacy import is_position_key

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AlsoSensorEntityDescription(SensorEntityDescription):
    """Describes an ALSO sensor; ``value_fn`` reads from the pyalso state."""

    value_fn: Callable[[VehicleState], StateType | datetime]
    attrs_fn: Callable[[VehicleState], dict[str, Any]] | None = None


#: Charge-state options known up front. Anything else the stream reports is exposed as
#: ``unknown_<n>`` (see :func:`charge_state_option`) and added to the options when it shows up.
CHARGE_STATE_OPTIONS = ["not_charging", "fully_charged"]


def charge_state_option(state: VehicleState) -> str | None:
    """The stream's charging state as an enum-sensor option, ``None`` before any push data.

    Only the values verified by observation get a name (1 = not_charging, 3 = fully_charged). Every
    other number, including the schema's CHARGING (2) and CHARGING_ISSUE (4), is ``unknown_<n>``
    until it has been observed. The REST shadow's ``con_NChargeState`` uses a DIFFERENT numbering
    and is never used here.
    """
    charging = state.charging_state
    if charging is None:
        return None
    if isinstance(charging, BatteryChargingState) and charging.verified:
        return charging.name.lower()
    return f"unknown_{int(charging)}"


SENSORS: tuple[AlsoSensorEntityDescription, ...] = (
    AlsoSensorEntityDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery_level,
    ),
    AlsoSensorEntityDescription(
        key="charge_limit",
        translation_key="charge_limit",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.charge_limit,
    ),
    AlsoSensorEntityDescription(
        key="battery_level_real",
        translation_key="battery_level_real",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.battery_level_real,
    ),
    # Verified: the REST odometer equals the stream's value, which is in miles. Home Assistant
    # converts it for display according to the unit system.
    AlsoSensorEntityDescription(
        key="odometer",
        translation_key="odometer",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.odometer_miles,
    ),
    # Delivered by the push stream only: ``None`` until the first battery packet arrives.
    AlsoSensorEntityDescription(
        key="minutes_until_full",
        translation_key="minutes_until_full",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.minutes_till_full,
    ),
    AlsoSensorEntityDescription(
        key="battery_health",
        translation_key="battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery_health,
    ),
    AlsoSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.firmware_version,
    ),
    # Charge state from the PUSH stream (``BatteryState.charging_state``). Unknown until the stream
    # delivered a battery packet. Unverified numbers are ``unknown_<n>``, never guessed.
    AlsoSensorEntityDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_STATE_OPTIONS,
        value_fn=charge_state_option,
        attrs_fn=lambda s: {"charging_state_raw": s.battery_charging_state_raw},
    ),
    # The REST shadow's ``con_NChargeState``: a different enum from the stream's charging state
    # (unplugged and idle read 0 here but 1 = not_charging there), so only the raw number is shown.
    AlsoSensorEntityDescription(
        key="charge_state_raw",
        translation_key="charge_state_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.charge_state_raw,
    ),
    AlsoSensorEntityDescription(
        key="power_state_raw",
        translation_key="power_state_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.power_state_raw,
    ),
    AlsoSensorEntityDescription(
        key="ride_mode_raw",
        translation_key="ride_mode_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.ride_mode_raw,
    ),
    AlsoSensorEntityDescription(
        key="fault_count",
        translation_key="fault_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.fault_count,
        attrs_fn=lambda s: {"fault_level_raw": s.fault_level_raw},
    ),
    AlsoSensorEntityDescription(
        key="gps_satellites",
        translation_key="gps_satellites",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.satellite_count,
    ),
    AlsoSensorEntityDescription(
        key="last_location_update",
        translation_key="last_location_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_updated,
    ),
)


def discoverable_shadow_keys(state: VehicleState) -> set[str]:
    """Shadow keys that should get an auto-discovered sensor.

    Numeric and boolean values only; nested objects, lists and strings are skipped, as is
    everything that already has an explicit entity. Anything that looks like position data is never
    turned into a sensor (privacy), whatever the key is called.
    """
    keys: set[str] = set()
    for key, value in state.shadow.items():
        if key in EXPLICIT_SHADOW_KEYS or not _is_scalar_number(value):
            continue
        if is_position_key(key):
            continue
        keys.add(key)
    return keys


def _is_scalar_number(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return isinstance(value, int | float) and math.isfinite(value)


async def async_setup_entry(
    hass: HomeAssistant, entry: AlsoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the sensors, including auto-discovered ones."""
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    known_vehicles: set[str] = set()
    created_shadow: set[tuple[str, str]] = set()

    @callback
    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for vid, state in coordinator.data.items():
            if vid not in known_vehicles:
                known_vehicles.add(vid)
                entities.extend(
                    (AlsoChargeStateSensor if desc.key == "charge_state" else AlsoSensor)(
                        coordinator, vid, desc
                    )
                    for desc in SENSORS
                )
                # Bring back discovered sensors whose key is temporarily missing from the shadow.
                prefix = f"{vid}_{SHADOW_UNIQUE_ID_INFIX}"
                for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
                    if reg.domain == "sensor" and reg.unique_id.startswith(prefix):
                        key = reg.unique_id.removeprefix(prefix)
                        if (vid, key) not in created_shadow:
                            created_shadow.add((vid, key))
                            entities.append(AlsoShadowSensor(coordinator, vid, key))
            for key in sorted(discoverable_shadow_keys(state)):
                if (vid, key) not in created_shadow:
                    created_shadow.add((vid, key))
                    entities.append(AlsoShadowSensor(coordinator, vid, key))
        if entities:
            async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AlsoSensor(AlsoEntity, SensorEntity):
    """A sensor described by an :class:`AlsoSensorEntityDescription`."""

    entity_description: AlsoSensorEntityDescription

    def __init__(
        self,
        coordinator: AlsoCoordinator,
        vehicle_id: str,
        description: AlsoSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, vehicle_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        state = self.vehicle_state
        return self.entity_description.value_fn(state) if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.vehicle_state
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(state) if state and attrs_fn else None


class AlsoChargeStateSensor(AlsoSensor):
    """Enum sensor whose options grow by an ``unknown_<n>`` value when the stream sends one."""

    @property
    def options(self) -> list[str]:
        options = list(CHARGE_STATE_OPTIONS)
        value = self.native_value
        if isinstance(value, str) and value not in options:
            options.append(value)
        return options


class AlsoShadowSensor(AlsoEntity, SensorEntity):
    """Auto-discovered raw shadow value: disabled by default, no unit, no device class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str, shadow_key: str) -> None:
        super().__init__(coordinator, vehicle_id, f"{SHADOW_UNIQUE_ID_INFIX}{shadow_key}")
        self._shadow_key = shadow_key
        self._attr_name = shadow_key

    @property
    def native_value(self) -> StateType:
        state = self.vehicle_state
        value = state.shadow_value(self._shadow_key) if state else None
        if not _is_scalar_number(value):
            return None
        return int(value) if isinstance(value, bool) else value
