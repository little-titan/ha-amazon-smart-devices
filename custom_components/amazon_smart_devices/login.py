"""Two-step Amazon login: credentials first, one-time code second.

``aioamazondevices`` exposes a single ``login_mode_interactive(otp_code)``
call that posts the password and the code back to back, so the code has to be
typed before Amazon has even looked at the password. Amazon's own sign-in
works the other way round: password, then a code page. This module splits the
library's login at that seam so the config flow can do the same.

It replays the library's private login steps verbatim (see
``login_mode_interactive`` and ``_login_mode_interactive_oauth`` in
``aioamazondevices/login.py``). To make that safe, it only runs when the
library's login code is exactly the code that was mirrored: an AST
fingerprint of those functions is compared at runtime. On any mismatch
``supported`` is False and callers use ``OneShotLogin``, which defers to the
library's public API and only differs in that the password is checked
together with the code instead of before it.

No Home Assistant imports here, so ``scripts/discover.py`` can use it too.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import logging
import textwrap
from typing import TYPE_CHECKING, Any

from aioamazondevices.exceptions import CannotAuthenticate

if TYPE_CHECKING:
    from aioamazondevices.api import AmazonEchoApi
    from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

# AST fingerprint of AmazonLogin.{login_mode_interactive,
# _login_mode_interactive_oauth, _oauth_login}. Identical in aioamazondevices
# 16.0.0 through 16.5.0. Update after re-checking this module against a new
# library release; tests/test_login.py fails loudly when it drifts.
MIRRORED_LOGIN_FINGERPRINT = "ffa42b14e50975e7"
_FINGERPRINTED = (
    "login_mode_interactive",
    "_login_mode_interactive_oauth",
    "_oauth_login",
)
_REQUIRED_PRIVATE = (
    "_create_code_verifier",
    "_oauth_login",
    "_get_request_from_soup",
    "_get_inputs_from_soup",
    "_register_device",
    "_domain_refresh_auth_cookies",
    "_register_device_capabilities",
    "obtain_account_customer_id",
)

OTP_INPUT_ID = "auth-mfa-otpcode"
CAPTCHA_FORM_IDS = ("cvf-aamation-challenge-form", "auth-captcha-image-container")


class InvalidCredentials(CannotAuthenticate):
    """Amazon rejected the email or password."""


class InvalidOtp(CannotAuthenticate):
    """Amazon rejected the one-time code; a new one may be entered."""


class CaptchaRequired(CannotAuthenticate):
    """Amazon answered with a captcha instead of the code page."""


class OtpPageMissing(CannotAuthenticate):
    """Password accepted but no code page (2-Step Verification off, or an
    unsupported challenge such as approve-on-phone)."""


def login_fingerprint() -> str | None:
    """Return the AST fingerprint of the installed library's login code."""
    try:
        from aioamazondevices import login as lib_login  # noqa: PLC0415

        source = textwrap.dedent(inspect.getsource(lib_login.AmazonLogin))
        cls = ast.parse(source).body[0]
    except (ImportError, OSError, TypeError, IndexError, SyntaxError):
        return None
    digest = hashlib.sha256()
    for node in getattr(cls, "body", []):
        if getattr(node, "name", None) in _FINGERPRINTED:
            digest.update(ast.dump(node, include_attributes=False).encode())
    return digest.hexdigest()[:16]


def two_step_supported() -> bool:
    """Return True if the installed library matches the mirrored login code."""
    return login_fingerprint() == MIRRORED_LOGIN_FINGERPRINT


class TwoStepLogin:
    """Password first, code second, on one AmazonEchoApi instance.

    The instance (and its aiohttp session cookies) must live from
    ``async_submit_credentials`` until ``async_submit_otp`` returns.
    """

    supported = True

    def __init__(self, api: AmazonEchoApi) -> None:
        """Wrap an AmazonEchoApi built with the user's email and password."""
        self._api = api
        self._verifier: bytes | None = None
        self._otp_page: BeautifulSoup | None = None

    async def async_submit_credentials(self) -> None:
        """Post email + password; succeed only if Amazon shows the code page.

        Mirrors the first half of ``_login_mode_interactive_oauth``,
        including its retry in Japanese when Amazon serves a captcha.
        """
        login = self._api.login
        self._verifier = login._create_code_verifier()  # noqa: SLF001
        _, soup = await login._oauth_login(self._verifier)  # noqa: SLF001
        if soup.find("form", id="cvf-aamation-challenge-form"):
            _LOGGER.debug("Captcha challenge found, retrying with Japanese")
            _, soup = await login._oauth_login(  # noqa: SLF001
                self._verifier, registration_language="ja_JP"
            )
        self._otp_page = _classify_after_password(soup)

    async def async_submit_otp(self, otp_code: str) -> dict[str, Any]:
        """Post the code and finish device registration; return login data.

        Mirrors the second half of ``_login_mode_interactive_oauth`` and the
        rest of ``login_mode_interactive``. On a wrong code Amazon serves the
        code page again; it is kept so the user can simply retry.
        """
        if self._otp_page is None or self._verifier is None:
            raise CannotAuthenticate("Credentials step has not completed")

        login = self._api.login
        wrapper = self._api._http_wrapper  # noqa: SLF001
        state = self._api._session_state_data  # noqa: SLF001

        method, url = login._get_request_from_soup(self._otp_page)  # noqa: SLF001
        inputs = login._get_inputs_from_soup(self._otp_page)  # noqa: SLF001
        inputs["otpCode"] = otp_code.strip()
        inputs["mfaSubmit"] = "Submit"
        inputs["rememberDevice"] = "false"

        soup, resp = await wrapper.session_request(
            method=method, url=url, input_data=inputs
        )
        auth_code = resp.url.query.get("openid.oa2.authorization_code")
        if auth_code is None:
            if soup.find("input", id=OTP_INPUT_ID):
                self._otp_page = soup
                raise InvalidOtp("Amazon rejected the one-time code")
            raise CannotAuthenticate("No authorization code after code page")

        login_data = await login._register_device(  # noqa: SLF001
            {
                "authorization_code": auth_code,
                "code_verifier": self._verifier,
                "domain": state.domain,
            }
        )
        state.login_stored_data = login_data
        await login._domain_refresh_auth_cookies()  # noqa: SLF001
        await login._register_device_capabilities()  # noqa: SLF001
        await login.obtain_account_customer_id()
        state.login_stored_data.update(
            {"site": state.retail_site_url.human_repr().rstrip("/")}
        )
        self._otp_page = None
        return state.login_stored_data


class OneShotLogin:
    """Fallback with the same two-call shape over the library's public API.

    The password is held until the code arrives and both go to Amazon
    together, so a wrong password is only reported after the code step.
    """

    supported = False

    def __init__(self, api: AmazonEchoApi) -> None:
        """Wrap an AmazonEchoApi built with the user's email and password."""
        self._api = api

    async def async_submit_credentials(self) -> None:
        """Nothing to send yet."""

    async def async_submit_otp(self, otp_code: str) -> dict[str, Any]:
        """Run the library's single-call interactive login."""
        return await self._api.login.login_mode_interactive(otp_code.strip())


def create_login(api: AmazonEchoApi) -> TwoStepLogin | OneShotLogin:
    """Return the two-step login if the library matches, else the fallback."""
    if two_step_supported() and all(
        hasattr(api.login, name) for name in _REQUIRED_PRIVATE
    ):
        return TwoStepLogin(api)
    _LOGGER.warning(
        "Installed aioamazondevices login code differs from the version this "
        "integration mirrors (fingerprint %s); using one-shot login",
        login_fingerprint(),
    )
    return OneShotLogin(api)


def _classify_after_password(soup: BeautifulSoup) -> BeautifulSoup:
    """Return the code page, or raise the reason Amazon did not show it."""
    if soup.find("input", id=OTP_INPUT_ID):
        return soup
    if any(soup.find(id=form_id) for form_id in CAPTCHA_FORM_IDS):
        raise CaptchaRequired("Amazon requested a captcha")
    if soup.find(id="auth-error-message-box") or soup.find(
        "input", {"name": "password"}
    ):
        raise InvalidCredentials("Amazon rejected the email or password")
    raise OtpPageMissing("Amazon did not show a one-time code page")
