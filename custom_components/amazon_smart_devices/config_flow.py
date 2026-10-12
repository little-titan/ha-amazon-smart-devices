"""Config flow for Amazon Smart Devices.

Two screens, like Amazon's own sign-in:

1. ``user`` (or ``reauth_confirm``): email + password are posted to Amazon.
   The flow only moves on once Amazon answers with its one-time-code page,
   so a wrong password is reported here, before any code is asked for.
2. ``otp``: the code is posted on the same session. A wrong code keeps the
   user on this screen to try a fresh one.

The login itself is aioamazondevices' mobile-app OAuth + device
registration, split in two by ``login.py``. See README "Authentication".
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRetrieveData,
)
from aioamazondevices.structures import AmazonSaveDataConfig
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import AlexaSmartHomeClient, device_kind, summarize_endpoints
from .const import CONF_LOGIN_DATA, DOMAIN, LOGGER
from .login import (
    CaptchaRequired,
    InvalidCredentials,
    InvalidOtp,
    OneShotLogin,
    OtpPageMissing,
    TwoStepLogin,
    create_login,
)

_EMAIL = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
_OTP = TextSelector(TextSelectorConfig(autocomplete="one-time-code"))

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): _EMAIL, vol.Required(CONF_PASSWORD): _PASSWORD}
)
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD})
STEP_OTP_SCHEMA = vol.Schema({vol.Required(CONF_CODE): _OTP})


# Most specific first: the login errors all subclass CannotAuthenticate.
_ERROR_KEYS: tuple[tuple[type[Exception] | tuple[type[Exception], ...], str], ...] = (
    (InvalidOtp, "invalid_otp"),
    (InvalidCredentials, "invalid_auth"),
    (CaptchaRequired, "captcha_required"),
    (OtpPageMissing, "otp_page_missing"),
    (CannotAuthenticate, "invalid_auth"),
    (CannotConnect, "cannot_connect"),
    ((CannotRetrieveData, ValueError), "cannot_retrieve_data"),
)


def _error_key(err: Exception) -> str:
    """Map a login exception to a strings.json error key."""
    for exc_type, key in _ERROR_KEYS:
        if isinstance(err, exc_type):
            return key
    LOGGER.exception("Unexpected error during Amazon login", exc_info=err)
    return "unknown"


class AmazonSmartDevicesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Amazon Smart Devices."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._username: str | None = None
        self._api: AmazonEchoApi | None = None
        self._login: TwoStepLogin | OneShotLogin | None = None

    async def _async_start_login(self, username: str, password: str) -> None:
        """Create a fresh session and post the credentials.

        The session lives on the flow until the code step finishes: Amazon
        ties the code page to the cookies set by the password post.
        """
        self._username = username
        self._api = AmazonEchoApi(
            aiohttp_client.async_create_clientsession(self.hass),
            username,
            password,
            save_data=AmazonSaveDataConfig(path=Path(self.hass.config.path(DOMAIN))),
        )
        self._login = create_login(self._api)
        await self._login.async_submit_credentials()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_start_login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = _error_key(err)
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME)},
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when the stored Amazon tokens stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 for reauth: password only, email is fixed."""
        username = self._get_reauth_entry().data[CONF_USERNAME]
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_start_login(username, user_input[CONF_PASSWORD])
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                errors["base"] = _error_key(err)
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: username},
            errors=errors,
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: the one-time code Amazon is now asking for."""
        assert self._login is not None and self._api is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                login_data = await self._login.async_submit_otp(user_input[CONF_CODE])
                endpoints = await AlexaSmartHomeClient(self._api).async_list_endpoints()
            except Exception as err:  # noqa: BLE001 - mapped to a form error
                key = _error_key(err)
                # In one-shot mode the password is only checked now; send the
                # user back to the screen where it can be corrected.
                if key == "invalid_auth" and isinstance(self._login, OneShotLogin):
                    return self._async_back_to_credentials(key)
                errors["base"] = key
            else:
                return await self._async_finish(login_data, endpoints)

        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            description_placeholders={CONF_USERNAME: self._username or ""},
            errors=errors,
        )

    def _async_back_to_credentials(self, error: str) -> ConfigFlowResult:
        step_id = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
        schema = STEP_REAUTH_SCHEMA if step_id == "reauth_confirm" else STEP_USER_SCHEMA
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_USERNAME: self._username}
            ),
            description_placeholders={CONF_USERNAME: self._username or ""},
            errors={"base": error},
        )

    async def _async_finish(
        self, login_data: dict[str, Any], endpoints: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        """Create or update the entry once Amazon issued tokens."""
        await self.async_set_unique_id(login_data["customer_info"]["user_id"])

        if self.source == SOURCE_REAUTH:
            # A device missing during reauth is not a reason to keep
            # broken tokens; the coordinator logs it after reload.
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_LOGIN_DATA: login_data}
            )

        self._abort_if_unique_id_configured()
        if not any(device_kind(e) for e in endpoints):
            summary = summarize_endpoints(endpoints)
            LOGGER.warning("No supported device found; endpoints: %s", summary)
            return self.async_abort(
                reason="no_supported_devices",
                description_placeholders={
                    "summary": ", ".join(f"{cat} ({n})" for cat, n in summary.items())
                    or "none"
                },
            )
        return self.async_create_entry(
            title=self._username or "",
            data={CONF_USERNAME: self._username, CONF_LOGIN_DATA: login_data},
        )
