"""Tests for setup, the coordinator and diagnostics."""

from __future__ import annotations

from unittest.mock import MagicMock

from aioamazondevices.exceptions import CannotAuthenticate, CannotConnect
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.amazon_smart_devices.api import AlexaGraphQLError, DeviceKind
from custom_components.amazon_smart_devices.const import DOMAIN

THERMO_ID = "amzn1.alexa.endpoint.11111111-2222-3333-4444-555555555555"
PLUG_ID = "amzn1.alexa.endpoint.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


async def test_setup_registers_device(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Setup logs in, discovers the thermostat, registers it and polls state."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED

    mock_echo_api.login.login_mode_stored_data.assert_awaited_once()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, THERMO_ID)})
    assert device is not None
    assert device.name == "Hallway Thermostat"
    assert device.manufacturer == "Amazon"
    assert device.model == "Amazon Smart Thermostat"

    coordinator = config_entry.runtime_data
    assert coordinator.ids_of(DeviceKind.THERMOSTAT) == [THERMO_ID]
    assert coordinator.ids_of(DeviceKind.SMART_PLUG) == [PLUG_ID]
    assert THERMO_ID in coordinator.data
    mock_client.get.assert_awaited_with([THERMO_ID, PLUG_ID], rich=True)

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Rejected stored tokens put the entry into reauth."""
    mock_echo_api.login.login_mode_stored_data.side_effect = CannotAuthenticate("x")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_connection_failure_retries(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Network failure at setup -> setup retry."""
    mock_client.list.side_effect = CannotConnect("x")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rich_query_fallback(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    states,
) -> None:
    """A schema-rejected rich query falls back to the minimal one."""
    mock_client.get.side_effect = [
        AlexaGraphQLError([{"message": "Unknown type"}]),
        states,
    ]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    assert coordinator.rich_state_supported is False
    assert mock_client.get.await_args_list[-1].kwargs == {"rich": False}


async def test_diagnostics_redacts(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Diagnostics carry the raw payload with identifiers redacted."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    text = str(diag)
    assert THERMO_ID not in text
    assert "G0000000000000AB" not in text
    assert PLUG_ID not in text
    assert "user@example.com" not in text
    assert "refresh_token" not in text
    assert diag["endpoint_summary"] == {"LIGHT": 1, "SMARTPLUG": 2, "THERMOSTAT": 1}
    assert diag["device_kinds"] == ["smart_plug", "thermostat"]
    assert diag["device_state"][0]["features"][0]["name"] == "thermostat"
    assert [a["friendlyName"] for a in diag["phoenix_devices"]] == [
        "Hallway Thermostat",
        "Coffee Maker",
    ]
