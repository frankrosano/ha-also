"""Fake data and helpers shared by the tests. Every value here is invented."""

from __future__ import annotations

import copy
from typing import Any

from pyalso import (
    BatteryState,
    BatteryStateEvent,
    BikeInfoEvent,
    BikeLocation,
    BikeLocationEvent,
    Connectivity,
    Location,
    OdometerEvent,
    UnknownPacket,
    User,
    Vehicle,
    VehicleState,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

EMAIL = "user@example.com"
USER_UUID = "00000000-0000-4000-8000-000000000001"
USER_REFERENCE_ID = "USR-AAAA-BBBB-CCCC"
VEHICLE_ID = "VHC-AAAA-BBBB-CCCC"
VEHICLE_ID_2 = "VHC-AAAA-BBBB-DDDD"
SERIAL = "TESTSERIAL0000001"
BIKE_NAME = "Test Bike"
LAT_NANO = 1_234_567_890
LON_NANO = -9_876_543_210
ACCESS_TOKEN = "fake-access-token-1"
REFRESH_TOKEN = "fake-refresh-token-1"
ID_TOKEN = "fake-id-token-1"
EXPIRES_AT = 4_000_000_000.0

USER_DICT: dict[str, Any] = {
    "user": {
        "user_id": 4242,
        "user_reference_id": USER_REFERENCE_ID,
        "user_uuid": USER_UUID,
        "given_name": "Test",
        "family_name": "Rider",
    },
    "primary_email": {"handle": EMAIL},
    "permissions": [],
}

SHADOW: dict[str, Any] = {
    "bms_NState": 2,
    "bms_rSOCChargeSetFb": 90,
    "bms_rSOCDisp": 87.5,
    "bms_rSOCReal": 91.25,
    "con_NBikePowerState": 1,
    "con_NChargeState": 3,
    "con_aLatitude": LAT_NANO,
    "con_aLongitude": LON_NANO,
    "con_nSatCount": 9,
    "con_tUtc": 1_700_000_000,
    "con_zAltitude": 1000,
    "hu_NModeSelectorKnob": 4,
    "hu_NRideModeREQ": 2,
    "hu_xOdometer": 123456,
    "pcm_NFault": {"NFaultCount": 1, "NFaultLevel": 2},
    "pcm_vVehicleFilt": 0,
}


def vehicle_dict(vehicle_id: str = VEHICLE_ID, serial: str = SERIAL) -> dict[str, Any]:
    return {
        "vehicle_reference_id": vehicle_id,
        "name": BIKE_NAME,
        "serial_number": serial,
        "vehicle_type": "TEST-MODEL",
        "lifecycle_state": "active",
        "created": "2024-01-01T00:00:00Z",
        "updated": "2024-01-02T00:00:00Z",
    }


def make_vehicle(vehicle_id: str = VEHICLE_ID, serial: str = SERIAL) -> Vehicle:
    return Vehicle.from_dict(vehicle_dict(vehicle_id, serial))


def make_state(
    vehicle_id: str = VEHICLE_ID,
    shadow_extra: dict[str, Any] | None = None,
    *,
    connected: bool | None = True,
) -> VehicleState:
    shadow = copy.deepcopy(SHADOW)
    shadow.update(shadow_extra or {})
    return VehicleState(
        vehicle_id=vehicle_id,
        vehicle=make_vehicle(vehicle_id),
        shadow=shadow,
        location=Location.from_dict(
            {
                "timestamp": "2024-05-01T12:00:00Z",
                "latitude": LAT_NANO,
                "longitude": LON_NANO,
                "altitude": 1000,
                "gps_info": {"accuracy": 5, "gps_time": 1_700_000_000, "satellite_count": 9},
            }
        ),
        connectivity=Connectivity.from_dict(
            {"connected": connected, "disconnect_reason": "TEST_REASON"}
        ),
    )


def make_user() -> User:
    return User.from_dict(USER_DICT)


def make_entry(
    *, data: dict[str, Any] | None = None, unique_id: str = USER_UUID, **overrides: Any
) -> MockConfigEntry:
    """A config entry like the ones written by 0.2.1 (no stored identity keys); ``data`` replaces
    the stored data entirely."""
    if data is None:
        data = {
            CONF_EMAIL: EMAIL,
            CONF_ACCESS_TOKEN: ACCESS_TOKEN,
            CONF_REFRESH_TOKEN: REFRESH_TOKEN,
            CONF_ID_TOKEN: ID_TOKEN,
            CONF_EXPIRES_AT: EXPIRES_AT,
        }
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=unique_id,
        data=data,
        **overrides,
    )


# ---------------------------------------------------------------- stream events (all invented)
PUSH_SERIAL = "PUSHSERIAL-BIKE-0001"
BATTERY_SERIAL = "BATTSERIAL-0001"
BASE_TIME = 1_700_000_000  # the REST report time (``con_tUtc``) of make_state()


def battery_event(
    level: int | None = 55,
    *,
    at: int | None = BASE_TIME + 100,
    minutes: int | None = 42,
    health: int | None = 98,
    charge_state: int | None = 1,
    limit: int | None = 90,
    unknown_fields: tuple[Any, ...] = (),
) -> BatteryStateEvent:
    battery = BatteryState(
        slot_raw=1,
        type_raw=1,
        serial_number=BATTERY_SERIAL,
        charging_state_raw=charge_state,
        level_percentage=level,
        minutes_till_full=minutes,
        charge_limit_percent=limit,
        battery_health=health,
        last_updated=at,
        bms_state_raw=2,
        unknown_fields=unknown_fields,
    )
    return BatteryStateEvent(
        feature=0x04,
        command=0x0101,
        status=0xFF,
        timestamp=at or BASE_TIME,
        payload=b"",
        batteries=(battery,),
    )


def location_event(
    lat_nano: int = 2_200_000_000, lon_nano: int = -3_300_000_000, *, at: int = BASE_TIME + 100
) -> BikeLocationEvent:
    location = BikeLocation(
        last_updated=at,
        latitude_nano=lat_nano,
        longitude_nano=lon_nano,
        altitude_raw=5000,
        azimuth_raw=90,
        velocity_raw=3.5,
        gps_accuracy_raw=4,
        satellite_count=11,
        gps_time=at,
        unknown_fields=(),
    )
    return BikeLocationEvent(
        feature=0x03, command=0x0100, status=0xFF, timestamp=at, payload=b"", location=location
    )


def odometer_event(miles: int) -> OdometerEvent:
    return OdometerEvent(
        feature=0x03,
        command=0x0103,
        status=0xFF,
        timestamp=BASE_TIME,
        payload=b"",
        odometer_mi=miles,
    )


def bike_info_event(
    model: str | None = "Test Model X", firmware: str | None = "9.8.7"
) -> BikeInfoEvent:
    return BikeInfoEvent(
        feature=0x03,
        command=0x0002,
        status=0x00,
        timestamp=BASE_TIME,
        payload=b"",
        model_name=model,
        serial_number=PUSH_SERIAL,
        odometer_mi=None,
        firmware_version=firmware,
    )


def unknown_packet(payload: bytes, *, feature: int = 0x77, command: int = 0x0105) -> UnknownPacket:
    return UnknownPacket(
        feature=feature,
        command=command,
        status=0xFF,
        timestamp=BASE_TIME,
        payload=payload,
        reason="unknown feature/command",
    )
