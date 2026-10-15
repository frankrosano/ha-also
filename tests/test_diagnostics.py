"""Diagnostics redaction tests."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.diagnostics import async_get_config_entry_diagnostics

from .common import (
    ACCESS_TOKEN,
    BIKE_NAME,
    EMAIL,
    ID_TOKEN,
    LAT_NANO,
    LON_NANO,
    REFRESH_TOKEN,
    SERIAL,
    USER_REFERENCE_ID,
    USER_UUID,
    VEHICLE_ID,
    make_entry,
    make_state,
)
from .conftest import PyalsoMocks


async def test_diagnostics_redacts_private_values(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    secret_extra = {
        "foo_aMysteryLatitude": 424242,
        "foo_NNewEnum": 7,
        "gps_other": 99,
    }
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, secret_extra
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)

    # Secrets and identifiers never appear.
    for secret in (
        EMAIL,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        ID_TOKEN,
        SERIAL,
        USER_UUID,
        USER_REFERENCE_ID,
        VEHICLE_ID,
        BIKE_NAME,
        str(LAT_NANO),
        str(LON_NANO),
        "1.23456789",
        "424242",
        "1000",
    ):
        assert secret not in dump, secret

    assert diag["entry"]["data"]["email"] == REDACTED
    assert diag["entry"]["data"]["access_token"] == REDACTED
    assert diag["entry"]["data"]["refresh_token"] == REDACTED
    assert diag["entry"]["data"]["id_token"] == REDACTED
    assert diag["entry"]["data"]["expires_at"] != REDACTED

    [vehicle] = diag["vehicles"]
    assert vehicle["vehicle"]["serial_number"] == REDACTED
    assert vehicle["vehicle"]["vehicle_reference_id"] == REDACTED
    shadow = vehicle["shadow"]
    assert shadow["con_aLatitude"] == REDACTED
    assert shadow["con_aLongitude"] == REDACTED
    assert shadow["con_zAltitude"] == REDACTED
    assert shadow["foo_aMysteryLatitude"] == REDACTED
    assert shadow["gps_other"] == REDACTED
    assert vehicle["location"]["latitude"] == REDACTED
    assert vehicle["location"]["longitude"] == REDACTED
    assert vehicle["location"]["altitude"] == REDACTED

    # The point of the download: the full raw shadow, unknown keys included, stays visible.
    assert shadow["foo_NNewEnum"] == 7
    assert shadow["bms_rSOCDisp"] == 87.5
    assert shadow["pcm_NFault"] == {"NFaultCount": 1, "NFaultLevel": 2}
    assert vehicle["location"]["gps_info"]["satellite_count"] == 9
    assert vehicle["connectivity"]["disconnect_reason"] == "TEST_REASON"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_entry_fixture(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert len(diag["vehicles"]) == 1
    assert diag["last_update_success"] is True


async def test_diagnostics_widened_position_and_secret_hints(
    hass: HomeAssistant, mock_pyalso: PyalsoMocks
) -> None:
    extra = {
        "con_aLat": 111_111,
        "con_aLon": 222_222,
        "xLngDeg": 333_333,
        "bike_sSerialNo": "SECRETSERIALNO",
        "foo_bPlatform": 5,  # contains "lat" but is not a position
        "foo_NDefault": 6,
    }
    mock_pyalso.client.get_vehicle_state.side_effect = lambda vid, vehicle=None: make_state(
        vid, extra
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diag, default=str)
    for secret in ("111111", "222222", "333333", "SECRETSERIALNO"):
        assert secret not in dump, secret
    shadow = diag["vehicles"][0]["shadow"]
    for key in ("con_aLat", "con_aLon", "xLngDeg", "bike_sSerialNo"):
        assert shadow[key] == REDACTED, key
    assert shadow["foo_bPlatform"] == 5
    assert shadow["foo_NDefault"] == 6
    await hass.config_entries.async_unload(entry.entry_id)
