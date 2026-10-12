"""Tests for the Amazon Smart Thermostat climate entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_smart_devices.api import THERMOSTAT_FEATURE

THERMO_ID = "amzn1.alexa.endpoint.11111111-2222-3333-4444-555555555555"
ENTITY_ID = "climate.hallway_thermostat"
SET_FEATURE = (
    "custom_components.amazon_smart_devices.api.AlexaSmartHomeClient.async_set_feature"
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # Alexa reports Fahrenheit
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_state(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Mode, measured temperature and target come from the Alexa state."""
    await _setup(hass, config_entry)
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes["current_temperature"] == 70
    assert state.attributes[ATTR_TEMPERATURE] == 68


async def test_set_temperature(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """The setpoint goes out in the unit Alexa reports and updates at once."""
    await _setup(hass, config_entry)
    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 72},
            blocking=True,
        )
    set_feature.assert_awaited_once_with(
        THERMO_ID,
        THERMOSTAT_FEATURE,
        "setTargetSetpoint",
        {"targetSetpoint": {"value": 72, "scale": "FAHRENHEIT"}},
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 72


async def test_set_hvac_mode(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """The mode goes out as setThermostatMode and updates at once."""
    await _setup(hass, config_entry)
    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.COOL},
            blocking=True,
        )
    set_feature.assert_awaited_once_with(
        THERMO_ID, THERMOSTAT_FEATURE, "setThermostatMode", {"thermostatMode": "COOL"}
    )
    assert hass.states.get(ENTITY_ID).state == HVACMode.COOL


async def test_command_failure_raises(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """A rejected command surfaces as HomeAssistantError and keeps the state."""
    await _setup(hass, config_entry)
    with (
        patch(SET_FEATURE, AsyncMock(side_effect=ValueError("nope"))),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 72},
            blocking=True,
        )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 68


async def test_auto_mode_and_action_and_limits(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    states: dict,
) -> None:
    """Auto maps to heat_cool; stages give the action; limits come from Alexa."""
    features = states[THERMO_ID]["features"]
    thermostat = next(f for f in features if f["name"] == "thermostat")
    next(p for p in thermostat["properties"] if p["name"] == "thermostatMode")[
        "thermostatModeValue"
    ] = "AUTO"
    thermostat["properties"].append(
        {
            "name": "allowedRange",
            "thermostatAllowedTemperatureRangeValue": {
                "heating": {
                    "minimum": {"value": 40, "scale": "FAHRENHEIT"},
                    "maximum": {"value": 87, "scale": "FAHRENHEIT"},
                },
                "cooling": {
                    "minimum": {"value": 55, "scale": "FAHRENHEIT"},
                    "maximum": {"value": 90, "scale": "FAHRENHEIT"},
                },
            },
        }
    )
    features.append(
        {
            "name": "hvac",
            "properties": [
                {"name": "coolerStage", "coolerStageValue": {"value": "OFF"}},
                {
                    "name": "primaryHeaterStage",
                    "primaryHeaterStageValue": {"value": "ON"},
                },
            ],
        }
    )
    thermostat["properties"] += [
        {
            "name": "lowerSetpoint",
            "value": {"value": 68, "scale": "FAHRENHEIT"},
        },
        {
            "name": "upperSetpoint",
            "value": {"value": 76, "scale": "FAHRENHEIT"},
        },
    ]
    await _setup(hass, config_entry)
    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes["target_temp_low"] == 68
    assert state.attributes["target_temp_high"] == 76
    assert state.attributes["hvac_action"] == "heating"
    assert state.attributes["min_temp"] == 40
    assert state.attributes["max_temp"] == 87


async def test_fan_mode(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Fan modes go out as mode/setMode on instance 5 and update at once."""
    await _setup(hass, config_entry)
    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_FAN_MODE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_FAN_MODE: "circulate"},
            blocking=True,
        )
    set_feature.assert_awaited_once_with(
        THERMO_ID, "mode", "setMode", {"mode": "3"}, instance="5"
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_FAN_MODE] == "circulate"


async def test_set_auto_setpoints(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    """Low/high go out together as lowerSetpoint/upperSetpoint."""
    await _setup(hass, config_entry)
    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, "target_temp_low": 70, "target_temp_high": 77},
            blocking=True,
        )
    set_feature.assert_awaited_once_with(
        THERMO_ID,
        THERMOSTAT_FEATURE,
        "setTargetSetpoint",
        {
            "lowerSetpoint": {"value": 70, "scale": "FAHRENHEIT"},
            "upperSetpoint": {"value": 77, "scale": "FAHRENHEIT"},
        },
    )


async def test_mode_switch_restores_remembered_setpoint(
    hass: HomeAssistant,
    mock_echo_api: MagicMock,
    mock_client: MagicMock,
    config_entry: MockConfigEntry,
    states: dict,
) -> None:
    """Heat and Cool each keep their own setpoint across mode switches."""
    await _setup(hass, config_entry)  # starts in Heat at 68
    with patch(SET_FEATURE, AsyncMock(return_value={})) as set_feature:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.COOL},
            blocking=True,
        )
        # No cool value known yet: only the mode goes out.
        assert set_feature.await_count == 1
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 76},
            blocking=True,
        )
        set_feature.reset_mock()
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.HEAT},
            blocking=True,
        )
    # Heat goes back to its own 68, not Cool's 76.
    set_feature.assert_awaited_with(
        THERMO_ID,
        THERMOSTAT_FEATURE,
        "setTargetSetpoint",
        {"targetSetpoint": {"value": 68.0, "scale": "FAHRENHEIT"}},
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 68
