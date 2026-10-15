"""Config flow, reauth and options flow tests."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pyalso import (
    AlsoConnectionError,
    AlsoInvalidCodeError,
    AlsoRateLimitError,
    OtpChallenge,
    Tokens,
    User,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.also.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .common import EMAIL, USER_REFERENCE_ID, USER_UUID, make_entry, make_user

# The stored format of the two immutable account identifiers (config entry data keys).
CONF_USER_UUID = "user_uuid"
CONF_USER_REFERENCE_ID = "user_reference_id"
NEW_TOKENS = Tokens("new-access", "new-refresh", "new-id", 4_100_000_000.0)
CHALLENGE = OtpChallenge(EMAIL, "fake-session-1", False, "EMAIL_OTP")


@dataclass
class FlowMocks:
    auth: MagicMock
    client: MagicMock


@pytest.fixture
def flow_mocks() -> Generator[FlowMocks]:
    auth = MagicMock()
    auth.request_otp = AsyncMock(return_value=CHALLENGE)
    auth.complete_otp = AsyncMock(return_value=NEW_TOKENS)
    auth.tokens = NEW_TOKENS
    client = MagicMock()
    client.get_user = AsyncMock(return_value=make_user())
    with (
        patch("custom_components.also.config_flow.AlsoAuth", return_value=auth),
        patch("custom_components.also.config_flow.AlsoClient", return_value=client),
        patch("custom_components.also.async_setup_entry", return_value=True),
    ):
        yield FlowMocks(auth, client)


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_user_flow_success(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"
    flow_mocks.auth.request_otp.assert_awaited_once_with(EMAIL)

    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["result"].unique_id == USER_UUID
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        # Both immutable identifiers are stored so a later reauth can match either one.
        CONF_USER_UUID: USER_UUID,
        CONF_USER_REFERENCE_ID: USER_REFERENCE_ID,
        CONF_ACCESS_TOKEN: "new-access",
        CONF_REFRESH_TOKEN: "new-refresh",
        CONF_ID_TOKEN: "new-id",
        CONF_EXPIRES_AT: 4_100_000_000.0,
    }
    flow_mocks.auth.complete_otp.assert_awaited_once_with(CHALLENGE, "123456")


async def test_unique_id_falls_back_to_reference_id(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    user = make_user()
    flow_mocks.client.get_user.return_value = User.from_dict(
        {"user": {"user_reference_id": "USR-AAAA-BBBB-DDDD"}}
    )
    assert user.user_uuid
    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["result"].unique_id == "USR-AAAA-BBBB-DDDD"


async def test_wrong_code_then_success_uses_fresh_session(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    err = AlsoInvalidCodeError(400, "BAD_CODE", "wrong code")
    err.session = "fresh-session-2"
    err.challenge = CHALLENGE.with_session("fresh-session-2")
    flow_mocks.auth.complete_otp.side_effect = [err, NEW_TOKENS]

    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"
    assert result["errors"] == {"base": "invalid_code"}

    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    retry_challenge = flow_mocks.auth.complete_otp.await_args_list[1].args[0]
    assert retry_challenge.session == "fresh-session-2"


async def test_rate_limited_on_request_otp(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_mocks.auth.request_otp.side_effect = AlsoRateLimitError(retry_after=30)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "rate_limited"}


async def test_rate_limited_on_complete_otp(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_mocks.auth.complete_otp.side_effect = AlsoRateLimitError()
    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["errors"] == {"base": "rate_limited"}


async def test_cannot_connect(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_mocks.auth.request_otp.side_effect = AlsoConnectionError()
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    assert result["errors"] == {"base": "cannot_connect"}

    flow_mocks.auth.request_otp.side_effect = None
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    assert result["step_id"] == "otp"

    flow_mocks.client.get_user.side_effect = AlsoConnectionError()
    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unexpected_error(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_mocks.auth.request_otp.side_effect = RuntimeError("boom")
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    assert result["errors"] == {"base": "unknown"}


async def test_invalid_email(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: "nope"})
    assert result["errors"] == {"base": "invalid_email"}
    flow_mocks.auth.request_otp.assert_not_awaited()


async def test_already_configured(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    make_entry().add_to_hass(hass)
    flow_id = await _start(hass)
    await hass.config_entries.flow.async_configure(flow_id, {CONF_EMAIL: EMAIL})
    result = await hass.config_entries.flow.async_configure(flow_id, {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success_updates_same_entry(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == EMAIL

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "reauth_otp"
    flow_mocks.auth.request_otp.assert_awaited_once_with(EMAIL)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_EMAIL] == EMAIL
    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert entry.data[CONF_EXPIRES_AT] == 4_100_000_000.0
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_wrong_account(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    flow_mocks.client.get_user.return_value = User.from_dict(
        {"user": {"user_uuid": "11111111-1111-4111-8111-111111111111"}}
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] != "new-access"


async def test_reauth_wrong_code(hass: HomeAssistant, flow_mocks: FlowMocks) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    err = AlsoInvalidCodeError(400, None, "wrong")
    err.challenge = CHALLENGE
    flow_mocks.auth.complete_otp.side_effect = err
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "0"})
    assert result["step_id"] == "reauth_otp"
    assert result["errors"] == {"base": "invalid_code"}


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 300}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 300}


@pytest.mark.parametrize("value", [59, 3601])
async def test_options_flow_rejects_out_of_range(hass: HomeAssistant, value: int) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: value}
        )


# ---------------------------------------------------------------- identity matching (review #10)
async def _reauth(hass: HomeAssistant, entry: MockConfigEntry) -> dict:  # type: ignore[type-arg]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})


async def test_reauth_accepts_the_same_account_when_the_id_that_keyed_the_entry_is_now_absent(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    """Regression: an entry keyed by the uuid was rejected (``wrong_account``) once a response
    only carried the reference id, because the 'preferred' id was recomputed and compared."""
    entry = make_entry(
        data={
            CONF_EMAIL: EMAIL,
            CONF_USER_UUID: USER_UUID,
            CONF_USER_REFERENCE_ID: USER_REFERENCE_ID,
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "old",
            CONF_ID_TOKEN: "old",
            CONF_EXPIRES_AT: 1.0,
        }
    )
    entry.add_to_hass(hass)
    assert entry.unique_id == USER_UUID
    flow_mocks.client.get_user.return_value = User.from_dict(
        {"user": {"user_reference_id": USER_REFERENCE_ID}}  # no uuid this time
    )
    result = await _reauth(hass, entry)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    assert entry.data[CONF_USER_UUID] == USER_UUID  # not lost: updates are merged


async def test_reauth_accepts_the_same_account_when_the_entry_was_keyed_by_the_reference_id(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    """The reverse drift: the entry was created before a uuid was exposed."""
    entry = make_entry(
        unique_id=USER_REFERENCE_ID,
        data={
            CONF_EMAIL: EMAIL,
            CONF_USER_REFERENCE_ID: USER_REFERENCE_ID,
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "old",
            CONF_ID_TOKEN: "old",
            CONF_EXPIRES_AT: 1.0,
        },
    )
    entry.add_to_hass(hass)
    flow_mocks.client.get_user.return_value = make_user()  # now carries BOTH ids
    result = await _reauth(hass, entry)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_USER_UUID] == USER_UUID  # the newly exposed id is remembered
    assert entry.unique_id == USER_REFERENCE_ID  # the unique id itself never changes


async def test_reauth_of_a_legacy_entry_without_stored_ids_matches_its_unique_id(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    entry = make_entry()  # unique_id is the uuid; no identity keys in the data (0.2.1 entries)
    entry.add_to_hass(hass)
    assert CONF_USER_UUID not in entry.data
    result = await _reauth(hass, entry)
    assert result["reason"] == "reauth_successful"


async def test_reauth_with_no_overlapping_identifier_is_still_a_different_account(
    hass: HomeAssistant, flow_mocks: FlowMocks
) -> None:
    entry = make_entry(
        data={
            CONF_EMAIL: EMAIL,
            CONF_USER_UUID: USER_UUID,
            CONF_USER_REFERENCE_ID: USER_REFERENCE_ID,
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "old",
            CONF_ID_TOKEN: "old",
            CONF_EXPIRES_AT: 1.0,
        }
    )
    entry.add_to_hass(hass)
    flow_mocks.client.get_user.return_value = User.from_dict(
        {
            "user": {
                "user_uuid": "11111111-1111-4111-8111-111111111111",
                "user_reference_id": "USR-AAAA-BBBB-DDDD",
            }
        }
    )
    result = await _reauth(hass, entry)
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == "old"
