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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from pyalso import (
    BatteryChargingState,
    BmsState,
    KnobPosition,
    MergedValue,
    PowerState,
    RideMode,
    VehicleState,
)

from .const import (
    EXPLICIT_PUSH_KEYS,
    EXPLICIT_SHADOW_KEYS,
    PUSH_UNIQUE_ID_INFIX,
    SHADOW_UNIQUE_ID_INFIX,
)
from .coordinator import AlsoConfigEntry, AlsoCoordinator
from .entity import AlsoEntity
from .privacy import is_position_key

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AlsoSensorEntityDescription(SensorEntityDescription):
    """Describes an ALSO sensor; ``value_fn`` reads from the pyalso state."""

    value_fn: Callable[[VehicleState], StateType | datetime]
    attrs_fn: Callable[[VehicleState], dict[str, Any]] | None = None


#: Charge-state options known up front: the three values verified by observation, plus the schema's
#: other numbers (0 unspecified, 4 CHARGING_ISSUE) under NEUTRAL names, because their meaning has
#: never been observed (see :func:`charge_state_option`).
#:
#: For every enum sensor (see :class:`AlsoEnumSensor`), a number outside its option list (a value a
#: future firmware sends, or one whose meaning is not established) is exposed as ``unknown_<n>``
#: and appended to the entity's options while it is the current state: the entity's option list
#: therefore grows by that value and shrinks again when the state moves on, and the value has no
#: translation (the UI shows the raw ``unknown_<n>``). Recorded history may mention such a state
#: that the current option list no longer lists.
CHARGE_STATE_OPTIONS = ["not_charging", "charging", "fully_charged", "unknown_0", "unknown_4"]


def charge_state_option(state: VehicleState) -> str | None:
    """The merged charging state as an enum-sensor option, ``None`` when it is unknown.

    pyalso merges the stream's ``charging_state`` with the REST ``con_NChargeState`` (stream first;
    the REST fallback translates only its observed numbers 0/2/3 into the stream's numbering, any
    other REST number is unknown). Only the values verified by observation get a name
    (1 = not_charging, 2 = charging, 3 = fully_charged). Every other stream number, including the
    schema's CHARGING_ISSUE (4), is ``unknown_<n>`` until it has been observed.
    """
    charging = state.charging_state
    if charging is None:
        return None
    if isinstance(charging, BatteryChargingState) and charging.verified:
        return charging.name.lower()
    return f"unknown_{int(charging)}"


# REST shadow enums with named states. Only the named numbers get an option; every other number
# (ride mode 0 = none selected, power states 0/1/5/6/7, bms 5, ...) is ``unknown_<n>``.
#: Ride mode (``hu_NRideModeREQ``): the app's RideMode names for every number except 0. Sport and
#: All Purpose are confirmed on-bike and All Purpose Manual was observed; the others are app names
#: the bike does not offer yet.
RIDE_MODE_NAMES: dict[int, str] = {
    mode: mode.name.lower() for mode in RideMode if mode is not RideMode.UNSPECIFIED
}
#: Power state (``con_NBikePowerState``): the three values verified by observation.
POWER_STATE_NAMES: dict[int, str] = {
    PowerState.TWELVEVOLTLOW: "asleep",
    PowerState.TWELVEVOLTHIGH: "awake_battery_off",
    PowerState.VBATTENABLED: "battery_on",
}
#: Display knob (``hu_NModeSelectorKnob``): the rotating display; every position was verified.
KNOB_POSITION_NAMES: dict[int, str] = {
    KnobPosition.DISPLAY_OFF: "display_off",
    KnobPosition.LOCKED: "locked",
    KnobPosition.READY: "ready",
    KnobPosition.RIDE: "ride",
}
#: BMS state (``bms_NState``): standby and active were verified; 5 (seen once) stays unknown.
BMS_STATE_NAMES: dict[int, str] = {BmsState.STANDBY: "standby", BmsState.ACTIVE: "active"}

RIDE_MODE_OPTIONS = list(RIDE_MODE_NAMES.values())
POWER_STATE_OPTIONS = list(POWER_STATE_NAMES.values())
KNOB_POSITION_OPTIONS = list(KNOB_POSITION_NAMES.values())
BMS_STATE_OPTIONS = list(BMS_STATE_NAMES.values())


def _named_option(value: int | None, names: dict[int, str]) -> str | None:
    """The option name of ``value``; ``unknown_<n>`` for an unnamed number, ``None`` if absent."""
    if value is None:
        return None
    return names.get(int(value), f"unknown_{int(value)}")


def ride_mode_option(state: VehicleState) -> str | None:
    """The REST ride mode as an enum-sensor option."""
    return _named_option(state.ride_mode, RIDE_MODE_NAMES)


def power_state_option(state: VehicleState) -> str | None:
    """The REST power state as an enum-sensor option."""
    return _named_option(state.power_state, POWER_STATE_NAMES)


def knob_position_option(state: VehicleState) -> str | None:
    """The REST display knob position as an enum-sensor option."""
    return _named_option(state.knob_position, KNOB_POSITION_NAMES)


def bms_state_option(state: VehicleState) -> str | None:
    """The REST BMS state as an enum-sensor option."""
    return _named_option(state.bms_state, BMS_STATE_NAMES)


def _rest_raw(value: int | None) -> dict[str, Any]:
    """The REST shadow's raw number of an enum sensor (these read the shadow only)."""
    return {"rest_raw": None if value is None else int(value)}


def _power_state_attrs(state: VehicleState) -> dict[str, Any]:
    """Raw number plus the app's PowerState name (``candidate_name``; may be unverified)."""
    power = state.power_state
    return {
        **_rest_raw(power),
        "candidate_name": power.name if isinstance(power, PowerState) else None,
    }


def _fault_attrs(state: VehicleState) -> dict[str, Any]:
    """Fault level plus the active fault codes, each mapped to the ISO time it was set."""
    codes = state.fault_codes
    return {
        "fault_level_raw": state.fault_level_raw,
        "fault_codes": (
            None
            if codes is None
            else {code: when.isoformat() if when else None for code, when in codes.items()}
        ),
    }


def _source_attrs(merged: MergedValue[Any]) -> dict[str, Any]:
    """Attributes of a merged (stream + REST) entity: the source used and both raw values."""
    return {"source": merged.source, "stream_raw": merged.stream_raw, "rest_raw": merged.rest_raw}


# Entities whose value both the stream and REST report are ONE entity each: pyalso uses the stream
# value while the stream is connected and delivered it, else the REST value. Their ``source``,
# ``stream_raw`` and ``rest_raw`` attributes show which one is shown and both raw values.
SENSORS: tuple[AlsoSensorEntityDescription, ...] = (
    AlsoSensorEntityDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery_level,
        attrs_fn=lambda s: _source_attrs(s.battery_level_merged),
    ),
    AlsoSensorEntityDescription(
        key="charge_limit",
        translation_key="charge_limit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.charge_limit,
        attrs_fn=lambda s: _source_attrs(s.charge_limit_merged),
    ),
    AlsoSensorEntityDescription(
        key="battery_level_real",
        translation_key="battery_level_real",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
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
        attrs_fn=lambda s: _source_attrs(s.odometer_merged),
    ),
    # Delivered by the push stream only: unknown until the first battery packet arrives, and
    # unknown (not 0) whenever the stream leaves the value out.
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
    # Charge state: the stream's ``charging_state`` merged with the REST ``con_NChargeState``
    # (different numbering; only the observed REST values 0/2/3 are translated). Unverified
    # numbers are ``unknown_<n>``, never guessed; ``rest_raw`` keeps the REST number.
    AlsoSensorEntityDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_STATE_OPTIONS,
        value_fn=charge_state_option,
        attrs_fn=lambda s: _source_attrs(s.charging_state_merged),
    ),
    # NOTE: the shadow's ``con_NBikePowerState`` and ``hu_NRideModeREQ`` (read here) are NOT the
    # stream's ``power_state_raw`` / ``ride_mode_raw`` (which pyalso keeps in ``state.push`` and
    # which come from different enums with their own, unobserved numbering), so they are NOT
    # merged. These entities read the SHADOW values only; the stream's are offered by the
    # auto-discovered ``push_*`` diagnostic sensors, named "(stream)".
    AlsoSensorEntityDescription(
        key="ride_mode",
        translation_key="ride_mode",
        device_class=SensorDeviceClass.ENUM,
        options=RIDE_MODE_OPTIONS,
        value_fn=ride_mode_option,
        attrs_fn=lambda s: _rest_raw(s.ride_mode),
    ),
    AlsoSensorEntityDescription(
        key="power_state",
        translation_key="power_state",
        device_class=SensorDeviceClass.ENUM,
        options=POWER_STATE_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=power_state_option,
        attrs_fn=_power_state_attrs,
    ),
    AlsoSensorEntityDescription(
        key="knob_position",
        translation_key="knob_position",
        device_class=SensorDeviceClass.ENUM,
        options=KNOB_POSITION_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=knob_position_option,
        attrs_fn=lambda s: _rest_raw(s.knob_position),
    ),
    AlsoSensorEntityDescription(
        key="bms_state",
        translation_key="bms_state",
        device_class=SensorDeviceClass.ENUM,
        options=BMS_STATE_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=bms_state_option,
        attrs_fn=lambda s: _rest_raw(s.bms_state),
    ),
    AlsoSensorEntityDescription(
        key="fault_count",
        translation_key="fault_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.fault_count,
        attrs_fn=_fault_attrs,
    ),
    AlsoSensorEntityDescription(
        key="gps_satellites",
        translation_key="gps_satellites",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.satellite_count,
        attrs_fn=lambda s: _source_attrs(s.satellite_count_merged),
    ),
    AlsoSensorEntityDescription(
        key="last_location_update",
        translation_key="last_location_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_updated,
        attrs_fn=lambda s: _source_attrs(s.last_updated_merged),
    ),
    # Speed: the stream's GPS velocity merged with the REST ``pcm_vVehicleFilt``. Both are mph,
    # verified against the display on a ride; Home Assistant converts it for display. A stopped
    # bike's stream fix omits the velocity, which pyalso reads as 0.0.
    AlsoSensorEntityDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.speed_mph,
        attrs_fn=lambda s: _source_attrs(s.speed_merged),
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


def discoverable_push_keys(state: VehicleState) -> set[str]:
    """Stream-only (``state.push``) keys that should get an auto-discovered sensor.

    The same rules as :func:`discoverable_shadow_keys`: numeric and boolean scalars only (serial
    numbers, firmware and model strings are skipped), nothing that already has an entity, and
    nothing that looks like position data. Units of these values are mostly unverified, so they
    are exposed raw in their own unique-id namespace.
    """
    keys: set[str] = set()
    for key, value in state.push.items():
        if key in EXPLICIT_PUSH_KEYS or not _is_scalar_number(value):
            continue
        if is_position_key(key):
            continue
        keys.add(key)
    return keys


async def async_setup_entry(
    hass: HomeAssistant, entry: AlsoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the sensors, including auto-discovered ones."""
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    known_vehicles: set[str] = set()
    created_shadow: set[tuple[str, str]] = set()
    created_push: set[tuple[str, str]] = set()

    @callback
    def _add_new() -> None:
        entities: list[SensorEntity] = []
        for vid, state in coordinator.data.items():
            if vid not in known_vehicles:
                known_vehicles.add(vid)
                entities.extend(
                    (AlsoEnumSensor if desc.device_class is SensorDeviceClass.ENUM else AlsoSensor)(
                        coordinator, vid, desc
                    )
                    for desc in SENSORS
                )
                # Bring back discovered sensors whose key is temporarily missing from the shadow
                # (never one for a key that now has an explicit, merged entity).
                prefix = f"{vid}_{SHADOW_UNIQUE_ID_INFIX}"
                for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
                    if reg.domain == "sensor" and reg.unique_id.startswith(prefix):
                        key = reg.unique_id.removeprefix(prefix)
                        if key in EXPLICIT_SHADOW_KEYS:
                            continue
                        if (vid, key) not in created_shadow:
                            created_shadow.add((vid, key))
                            entities.append(AlsoShadowSensor(coordinator, vid, key))
                push_prefix = f"{vid}_{PUSH_UNIQUE_ID_INFIX}"
                for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
                    if reg.domain == "sensor" and reg.unique_id.startswith(push_prefix):
                        key = reg.unique_id.removeprefix(push_prefix)
                        if key in EXPLICIT_PUSH_KEYS:
                            continue
                        if (vid, key) not in created_push:
                            created_push.add((vid, key))
                            entities.append(AlsoPushSensor(coordinator, vid, key))
            for key in sorted(discoverable_shadow_keys(state)):
                if (vid, key) not in created_shadow:
                    created_shadow.add((vid, key))
                    entities.append(AlsoShadowSensor(coordinator, vid, key))
            for key in sorted(discoverable_push_keys(state)):
                if (vid, key) not in created_push:
                    created_push.add((vid, key))
                    entities.append(AlsoPushSensor(coordinator, vid, key))
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


class AlsoEnumSensor(AlsoSensor):
    """Enum sensor whose options grow by the current ``unknown_<n>`` value.

    The description's options are the named states; a number without a name is shown as
    ``unknown_<n>`` and listed in the options only while it is the state.
    """

    @property
    def options(self) -> list[str]:
        options = list(self.entity_description.options or [])
        value = self.native_value
        if isinstance(value, str) and value not in options:
            options.append(value)
        return options


class AlsoShadowSensor(AlsoEntity, SensorEntity):
    """Auto-discovered raw shadow value: diagnostic, no unit, no device class."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

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


class AlsoPushSensor(AlsoEntity, SensorEntity):
    """Auto-discovered raw value that only the live stream delivers (``state.push``).

    Diagnostic, no unit, no device class: most of these units are unverified. The name says
    "(stream)" so it is never mistaken for the shadow value of a similarly named key.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AlsoCoordinator, vehicle_id: str, push_key: str) -> None:
        super().__init__(coordinator, vehicle_id, f"{PUSH_UNIQUE_ID_INFIX}{push_key}")
        self._push_key = push_key
        self._attr_name = f"{push_key} (stream)"

    @property
    def native_value(self) -> StateType:
        state = self.vehicle_state
        value = state.push.get(self._push_key) if state else None
        if not _is_scalar_number(value):
            return None
        return int(value) if isinstance(value, bool) else value
