"""Tests for the Amazon Smart Plug switch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aioamazondevices.exceptions import CannotConnect
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_smart_devices.api import POWER_FEATURE

PLUG_ID = "amzn1.alexa.endpoint.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ENTITY_ID = "switch.coffee_maker"
SET_FEATURE = (
    "custom_components.amazon_smart_devices.api.AlexaSmartHomeClient.async_set_feature"
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_only_amazon_plug_becomes_a_switch(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """The Amazon plug gets a switch; the Kasa plug and the light do not."""
    await _setup(hass, config_entry)
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "outlet"

    entries = er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )
    assert [(e.entity_id, e.unique_id) for e in entries] == [(ENTITY_ID, PLUG_ID)]


@pytest.mark.parametrize(
    ("service", "operation", "expected"),
    [(SERVICE_TURN_OFF, "turnOff", STATE_OFF), (SERVICE_TURN_ON, "turnOn", STATE_ON)],
)
async def test_turn_on_off(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    states: dict[str, dict[str, Any]],
    service: str,
    operation: str,
    expected: str,
) -> None:
    """Commands go out as power feature operations and update state at once."""
    # Start from the opposite state so the optimistic update is visible.
    start = "ON" if expected == STATE_OFF else "OFF"
    states[PLUG_ID]["features"][0]["properties"][0]["powerStateValue"] = start
    await _setup(hass, config_entry)

    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )
    set_feature.assert_awaited_once_with(PLUG_ID, POWER_FEATURE, operation)
    assert hass.states.get(ENTITY_ID).state == expected


async def test_command_failure_raises(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """A rejected command surfaces as an error and leaves state unchanged."""
    await _setup(hass, config_entry)
    with (
        patch(SET_FEATURE, AsyncMock(side_effect=CannotConnect("down"))),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )
    assert exc.value.translation_key == "action_failed"
    assert hass.states.get(ENTITY_ID).state == STATE_ON


async def test_unreachable_plug_is_unavailable(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    states: dict[str, dict[str, Any]],
) -> None:
    """Alexa reporting the plug unreachable makes the switch unavailable."""
    states[PLUG_ID]["features"][1]["properties"][0]["reachabilityStatusValue"] = (
        "UNREACHABLE"
    )
    await _setup(hass, config_entry)
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_plug_added_later_gets_a_switch(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    endpoints: list[dict[str, Any]],
) -> None:
    """A plug that appears on re-discovery is added without a reload."""
    plug = next(e for e in endpoints if e["endpointId"] == PLUG_ID)
    mock_client.list.return_value = [e for e in endpoints if e is not plug]
    await _setup(hass, config_entry)
    assert hass.states.get(ENTITY_ID) is None

    mock_client.list.return_value = endpoints
    coordinator = config_entry.runtime_data
    await coordinator._async_discover()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON
