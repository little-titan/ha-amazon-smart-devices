"""Tests for the two-step login split."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aioamazondevices.exceptions import CannotAuthenticate
from bs4 import BeautifulSoup
import pytest
from yarl import URL

from custom_components.amazon_smart_devices import login as login_mod
from custom_components.amazon_smart_devices.login import (
    MIRRORED_LOGIN_FINGERPRINT,
    CaptchaRequired,
    InvalidCredentials,
    InvalidOtp,
    OneShotLogin,
    OtpPageMissing,
    TwoStepLogin,
    _classify_after_password,
    create_login,
    login_fingerprint,
)

OTP_PAGE = """<form name="signIn" method="post" action="https://www.amazon.com/ap/signin">
<input type="hidden" name="appActionToken" value="tok">
<input id="auth-mfa-otpcode" name="otpCode"></form>"""
PASSWORD_PAGE = """<div id="auth-error-message-box">Your password is incorrect</div>
<form name="signIn"><input name="email"><input name="password" type="password"></form>"""
CAPTCHA_PAGE = '<form id="cvf-aamation-challenge-form"></form>'
APPROVE_PAGE = "<div>Approve the notification sent to your phone</div>"


def soup(html: str) -> BeautifulSoup:
    """Parse HTML the way aioamazondevices does."""
    return BeautifulSoup(html, "html.parser")


def test_fingerprint_matches_installed_library() -> None:
    """Drift guard: fails when aioamazondevices changes its login code.

    If this fails after a library upgrade, diff aioamazondevices/login.py
    against login.py here, re-sync, then update MIRRORED_LOGIN_FINGERPRINT.
    Until then the integration safely uses the one-shot fallback.
    """
    assert login_fingerprint() == MIRRORED_LOGIN_FINGERPRINT


@pytest.mark.parametrize(
    ("html", "error"),
    [
        (PASSWORD_PAGE, InvalidCredentials),
        (CAPTCHA_PAGE, CaptchaRequired),
        (APPROVE_PAGE, OtpPageMissing),
    ],
)
def test_classify_after_password_errors(html: str, error: type[Exception]) -> None:
    """Each non-code page maps to a distinct, user-actionable error."""
    with pytest.raises(error):
        _classify_after_password(soup(html))


def test_classify_after_password_otp_page() -> None:
    """The code page is returned for the next step."""
    page = soup(OTP_PAGE)
    assert _classify_after_password(page) is page


def _api(responses: list[tuple[BeautifulSoup, Any]]) -> MagicMock:
    """Build an AmazonEchoApi double whose login helpers are the real ones."""
    from aioamazondevices.login import AmazonLogin  # noqa: PLC0415

    api = MagicMock()
    real = AmazonLogin.__new__(AmazonLogin)
    login = api.login
    login._create_code_verifier = MagicMock(return_value=b"verifier")
    login._get_request_from_soup = real._get_request_from_soup
    login._get_inputs_from_soup = real._get_inputs_from_soup
    login._register_device = AsyncMock(return_value={"customer_info": {"user_id": "u"}})
    login._domain_refresh_auth_cookies = AsyncMock()
    login._register_device_capabilities = AsyncMock()
    login.obtain_account_customer_id = AsyncMock()
    api._http_wrapper.session_request = AsyncMock(side_effect=responses)
    api._session_state_data = SimpleNamespace(
        domain="com",
        retail_site_url=URL("https://www.amazon.com/"),
        login_stored_data={},
    )
    return api


def _resp(url: str) -> SimpleNamespace:
    return SimpleNamespace(url=URL(url))


async def test_two_step_happy_path() -> None:
    """Credentials, then code, then the library's registration sequence."""
    landing = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=AUTH"
    api = _api([(soup(""), _resp(landing))])
    api.login._oauth_login = AsyncMock(return_value=(URL("x"), soup(OTP_PAGE)))
    two_step = TwoStepLogin(api)

    await two_step.async_submit_credentials()
    api._http_wrapper.session_request.assert_not_called()  # no code sent yet

    data = await two_step.async_submit_otp(" 123456 ")
    sent = api._http_wrapper.session_request.call_args.kwargs
    assert sent["method"] == "post"
    assert sent["url"] == URL("https://www.amazon.com/ap/signin")
    assert sent["input_data"] == {
        "appActionToken": "tok",
        "otpCode": "123456",
        "mfaSubmit": "Submit",
        "rememberDevice": "false",
    }
    api.login._register_device.assert_awaited_once_with(
        {"authorization_code": "AUTH", "code_verifier": b"verifier", "domain": "com"}
    )
    api.login._domain_refresh_auth_cookies.assert_awaited_once()
    api.login._register_device_capabilities.assert_awaited_once()
    api.login.obtain_account_customer_id.assert_awaited_once()
    assert data == {"customer_info": {"user_id": "u"}, "site": "https://www.amazon.com"}


async def test_two_step_captcha_retries_in_japanese_then_succeeds() -> None:
    """Mirror the library: captcha -> retry with ja_JP once."""
    api = _api([])
    api.login._oauth_login = AsyncMock(
        side_effect=[(URL("x"), soup(CAPTCHA_PAGE)), (URL("x"), soup(OTP_PAGE))]
    )
    await TwoStepLogin(api).async_submit_credentials()
    assert api.login._oauth_login.await_args_list[1].kwargs == {
        "registration_language": "ja_JP"
    }


async def test_two_step_wrong_code_then_retry() -> None:
    """A rejected code keeps the new code page so the next code can be tried."""
    retry_page = OTP_PAGE.replace('value="tok"', 'value="tok2"')
    landing = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=AUTH"
    api = _api(
        [
            (soup(retry_page), _resp("https://www.amazon.com/ap/mfa")),
            (soup(""), _resp(landing)),
        ]
    )
    api.login._oauth_login = AsyncMock(return_value=(URL("x"), soup(OTP_PAGE)))
    two_step = TwoStepLogin(api)
    await two_step.async_submit_credentials()

    with pytest.raises(InvalidOtp):
        await two_step.async_submit_otp("000000")
    api.login._register_device.assert_not_called()

    await two_step.async_submit_otp("123456")
    second = api._http_wrapper.session_request.call_args.kwargs["input_data"]
    assert second["appActionToken"] == "tok2"


async def test_otp_before_credentials_is_refused() -> None:
    """Calling the steps out of order is an error, not a half login."""
    with pytest.raises(CannotAuthenticate):
        await TwoStepLogin(MagicMock()).async_submit_otp("1")


async def test_create_login_falls_back_on_drift() -> None:
    """Any library drift selects the public one-shot login."""
    api = MagicMock()
    assert isinstance(create_login(api), TwoStepLogin)
    with patch.object(login_mod, "MIRRORED_LOGIN_FINGERPRINT", "different"):
        fallback = create_login(api)
    assert isinstance(fallback, OneShotLogin)

    api.login.login_mode_interactive = AsyncMock(return_value={"ok": 1})
    await fallback.async_submit_credentials()
    api.login.login_mode_interactive.assert_not_called()
    assert await fallback.async_submit_otp(" 42 ") == {"ok": 1}
    api.login.login_mode_interactive.assert_awaited_once_with("42")
