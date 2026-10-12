"""Fixtures for Amazon Smart Devices tests."""

from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_smart_devices.const import CONF_LOGIN_DATA, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
TEST_USERNAME = "user@example.com"
TEST_USER_ID = "amzn1.account.TESTUSER"
LOGIN_DATA: dict[str, Any] = {
    "customer_info": {"user_id": TEST_USER_ID},
    "site": "https://www.amazon.com",
}


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture."""
    data = json.loads((FIXTURES / name).read_text())
    data.pop("_note", None)
    return data


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom_components in every test."""


@pytest.fixture
def endpoints() -> list[dict[str, Any]]:
    """Return the synthetic endpoint list."""
    return load_fixture("list_endpoints.json")["data"]["listEndpoints"]["endpoints"]


@pytest.fixture
def states() -> dict[str, dict[str, Any]]:
    """Return synthetic thermostat state keyed by endpoint id."""
    eps = load_fixture("state.json")["data"]["listEndpoints"]["endpoints"]
    return {e["endpointId"]: e for e in eps}


@pytest.fixture
def mock_echo_api() -> Generator[MagicMock]:
    """Patch AmazonEchoApi everywhere it is constructed."""
    api = MagicMock()
    api.login.login_mode_interactive = AsyncMock(return_value=LOGIN_DATA)
    api.login.login_mode_stored_data = AsyncMock(return_value=LOGIN_DATA)
    with (
        patch(
            "custom_components.amazon_smart_devices.config_flow.AmazonEchoApi",
            return_value=api,
        ),
        patch(
            "custom_components.amazon_smart_devices.coordinator.AmazonEchoApi",
            return_value=api,
        ),
    ):
        yield api


@pytest.fixture
def mock_client(
    endpoints: list[dict[str, Any]], states: dict[str, dict[str, Any]]
) -> Generator[MagicMock]:
    """Patch the network methods of AlexaSmartHomeClient."""
    base = "custom_components.amazon_smart_devices.api.AlexaSmartHomeClient"
    with (
        patch(
            f"{base}.async_list_endpoints", AsyncMock(return_value=endpoints)
        ) as list_,
        patch(f"{base}.async_get_states", AsyncMock(return_value=states)) as get,
        patch(
            f"{base}.async_get_phoenix",
            AsyncMock(return_value=load_fixture("phoenix.json")),
        ),
    ):
        yield MagicMock(list=list_, get=get)


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TEST_USERNAME,
        unique_id=TEST_USER_ID,
        data={CONF_USERNAME: TEST_USERNAME, CONF_LOGIN_DATA: LOGIN_DATA},
    )
