"""Tests for the two-screen config flow."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aioamazondevices.exceptions import CannotAuthenticate, CannotConnect
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_smart_devices.const import CONF_LOGIN_DATA, DOMAIN
from custom_components.amazon_smart_devices.login import (
    CaptchaRequired,
    InvalidCredentials,
    InvalidOtp,
    OneShotLogin,
    OtpPageMissing,
)

from .conftest import LOGIN_DATA, TEST_USER_ID, TEST_USERNAME

CREDENTIALS = {CONF_USERNAME: TEST_USERNAME, CONF_PASSWORD: "pw"}
CODE = {CONF_CODE: "123456"}


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """Don't run entry setup after the flow creates an entry."""
    with patch(
        "custom_components.amazon_smart_devices.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.fixture
def mock_login() -> Generator[MagicMock]:
    """Patch the two-step login used by the flow."""
    login = MagicMock(supported=True)
    login.async_submit_credentials = AsyncMock()
    login.async_submit_otp = AsyncMock(return_value=LOGIN_DATA)
    with patch(
        "custom_components.amazon_smart_devices.config_flow.create_login",
        return_value=login,
    ):
        yield login


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert set(result["data_schema"].schema) == {CONF_USERNAME, CONF_PASSWORD}
    return result


async def test_two_screen_success(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
) -> None:
    """Password screen, then code screen, then an entry without the password."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    # Password went to Amazon; no code has been asked for or sent yet.
    mock_login.async_submit_credentials.assert_awaited_once()
    mock_login.async_submit_otp.assert_not_called()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"
    assert result["description_placeholders"] == {CONF_USERNAME: TEST_USERNAME}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    mock_login.async_submit_otp.assert_awaited_once_with("123456")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_USERNAME
    assert result["result"].unique_id == TEST_USER_ID
    assert result["data"] == {CONF_USERNAME: TEST_USERNAME, CONF_LOGIN_DATA: LOGIN_DATA}


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (InvalidCredentials("x"), "invalid_auth"),
        (CaptchaRequired("x"), "captcha_required"),
        (OtpPageMissing("x"), "otp_page_missing"),
        (CannotConnect("x"), "cannot_connect"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_credentials_errors_stay_on_first_screen(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
    side_effect: Exception,
    error: str,
) -> None:
    """Credential problems are reported before any code is requested."""
    mock_login.async_submit_credentials.side_effect = side_effect
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    mock_login.async_submit_otp.assert_not_called()

    mock_login.async_submit_credentials.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["step_id"] == "otp"


async def test_wrong_code_retry(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
) -> None:
    """A rejected code keeps the user on the code screen for the next code."""
    mock_login.async_submit_otp.side_effect = [InvalidOtp("x"), LOGIN_DATA]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["step_id"] == "otp"
    assert result["errors"] == {"base": "invalid_otp"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "654321"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_login.async_submit_credentials.assert_awaited_once()  # not re-sent


async def test_one_shot_fallback_bad_password_returns_to_first_screen(
    hass: HomeAssistant, mock_echo_api: MagicMock, mock_client: MagicMock
) -> None:
    """Fallback mode checks the password with the code; route errors back."""
    mock_echo_api.login.login_mode_interactive.side_effect = CannotAuthenticate("x")
    with patch(
        "custom_components.amazon_smart_devices.config_flow.create_login",
        side_effect=OneShotLogin,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        assert result["step_id"] == "otp"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_auth"}

        mock_echo_api.login.login_mode_interactive.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_supported_device(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
    endpoints: list[dict[str, Any]],
) -> None:
    """An account with only a light and a third-party plug aborts."""
    mock_client.list.return_value = endpoints[1:3]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_supported_devices"
    assert result["description_placeholders"] == {"summary": "LIGHT (1), SMARTPLUG (1)"}


async def test_already_configured(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Same Amazon account twice aborts."""
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_two_screens(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth: password screen, then code screen, then fresh tokens."""
    config_entry.add_to_hass(hass)
    new_login = LOGIN_DATA | {"refresh_token": "new"}
    mock_login.async_submit_otp.return_value = new_login

    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert set(result["data_schema"].schema) == {CONF_PASSWORD}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "pw"}
    )
    assert result["step_id"] == "otp"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_LOGIN_DATA] == new_login


async def test_reauth_wrong_account(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    mock_login: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth with another account is refused and tokens are kept."""
    config_entry.add_to_hass(hass)
    mock_login.async_submit_otp.return_value = {
        "customer_info": {"user_id": "someone-else"}
    }
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "pw"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CODE)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_LOGIN_DATA] == LOGIN_DATA
