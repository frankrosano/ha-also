"""Config flow for the ALSO integration (e-mail one-time-code login)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from pyalso import (
    AlsoApiError,
    AlsoAuth,
    AlsoClient,
    AlsoConnectionError,
    AlsoInvalidCodeError,
    AlsoRateLimitError,
    OtpChallenge,
    Tokens,
    User,
)

from .const import (
    CONF_EMAIL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import AlsoConfigEntry

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str})
STEP_OTP_SCHEMA = vol.Schema({vol.Required("code"): str})


class AlsoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ALSO."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth: AlsoAuth | None = None
        self._challenge: OtpChallenge | None = None
        self._email = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: AlsoConfigEntry) -> AlsoOptionsFlow:
        """Return the options flow."""
        return AlsoOptionsFlow()

    # ------------------------------------------------------------------ shared helpers
    async def _async_request_otp(self, email: str) -> str | None:
        """Send a code to ``email``. Returns an error key, or ``None`` on success."""
        self._auth = AlsoAuth(async_get_clientsession(self.hass))
        try:
            self._challenge = await self._auth.request_otp(email)
        except AlsoRateLimitError:
            return "rate_limited"
        except (AlsoConnectionError, AlsoApiError):
            return "cannot_connect"
        except Exception as err:
            _LOGGER.error("Unexpected error requesting a code (%s)", type(err).__name__)
            return "unknown"
        self._email = email
        return None

    async def _async_verify_code(self, code: str) -> tuple[str | None, Tokens | None, User | None]:
        """Exchange ``code`` for tokens and look up the user. Returns (error, tokens, user)."""
        auth, challenge = self._auth, self._challenge
        if auth is None or challenge is None:
            return "unknown", None, None
        try:
            await auth.complete_otp(challenge, code.strip())
            user = await AlsoClient(async_get_clientsession(self.hass), auth).get_user()
        except AlsoInvalidCodeError as err:
            # Retry with the fresh session the API handed back, if any.
            self._challenge = err.challenge or challenge
            return "invalid_code", None, None
        except AlsoRateLimitError:
            return "rate_limited", None, None
        except (AlsoConnectionError, AlsoApiError):
            return "cannot_connect", None, None
        except Exception as err:
            _LOGGER.error("Unexpected error verifying a code (%s)", type(err).__name__)
            return "unknown", None, None
        # Use the tokens held by ``auth``: they may have rotated during ``get_user``.
        return None, auth.tokens, user

    @staticmethod
    def _user_unique_id(user: User) -> str | None:
        return user.user_uuid or user.user_reference_id

    # --------------------------------------------------------------------- user flow
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the account e-mail and send a code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            if "@" not in email:
                errors["base"] = "invalid_email"
            elif error := await self._async_request_otp(email):
                errors["base"] = error
            else:
                return await self.async_step_otp()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_otp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the emailed code and create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error, tokens, user = await self._async_verify_code(user_input["code"])
            if error or tokens is None or user is None:
                errors["base"] = error or "unknown"
            elif (unique_id := self._user_unique_id(user)) is None:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._email,
                    data={CONF_EMAIL: self._email, **tokens.to_dict()},
                )
        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    # -------------------------------------------------------------------- reauth flow
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the session can no longer be refreshed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a new code to the e-mail of the existing entry."""
        email = self._get_reauth_entry().data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await self._async_request_otp(email):
                errors["base"] = error
            else:
                return await self.async_step_reauth_otp()
        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={"email": email},
        )

    async def async_step_reauth_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify the code and update the existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error, tokens, user = await self._async_verify_code(user_input["code"])
            if error or tokens is None or user is None:
                errors["base"] = error or "unknown"
            elif self._user_unique_id(user) != entry.unique_id:
                return self.async_abort(reason="wrong_account")
            else:
                return self.async_update_reload_and_abort(entry, data_updates=tokens.to_dict())
        return self.async_show_form(
            step_id="reauth_otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )


class AlsoOptionsFlow(OptionsFlow):
    """Options: polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
