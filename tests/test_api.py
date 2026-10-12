"""Tests for the HA-free Alexa thermostat client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.amazon_smart_devices.api import (
    QUERY_STATE_MINIMAL,
    QUERY_STATE_RICH,
    AlexaGraphQLError,
    AlexaSmartHomeClient,
    DeviceKind,
    device_kind,
    endpoint_categories,
    is_amazon_smart_plug,
    is_reachable,
    is_thermostat,
    parse_phoenix,
    phoenix_is_supported,
    phoenix_is_thermostat,
    power_state,
    summarize_endpoints,
    with_power_state,
)

from .conftest import load_fixture


def test_device_kind(endpoints: list[dict[str, Any]]) -> None:
    """Thermostat and Amazon plug are supported; light and Kasa plug are not."""
    assert [device_kind(e) for e in endpoints] == [
        DeviceKind.THERMOSTAT,
        None,
        None,
        DeviceKind.SMART_PLUG,
    ]


def test_amazon_plug_needs_amazon_manufacturer_and_power() -> None:
    """Category alone is not enough for a plug."""
    plug = {
        "displayCategories": {"primary": {"value": "SMARTPLUG"}},
        "manufacturer": {"value": {"text": "Amazon"}},
        "features": [{"name": "power"}],
    }
    assert is_amazon_smart_plug(plug)
    assert not is_amazon_smart_plug({**plug, "manufacturer": None})
    assert not is_amazon_smart_plug({**plug, "features": []})
    assert is_amazon_smart_plug(
        {**plug, "manufacturer": {"value": {"text": "Amazon Basics"}}}
    )


def test_power_and_reachability(states: dict[str, dict[str, Any]]) -> None:
    """Power and reachability are read from any feature carrying them."""
    plug = states["amzn1.alexa.endpoint.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    assert power_state(plug) is True
    assert is_reachable(plug) is True
    off = with_power_state(plug, on=False)
    assert power_state(off) is False
    assert power_state(plug) is True  # original untouched

    prop = plug["features"][0]["properties"][0]
    assert (
        power_state(
            {
                "features": [
                    {
                        "name": "power",
                        "properties": [{**prop, "powerStateValue": "WEIRD"}],
                    }
                ]
            }
        )
        is None
    )
    assert (
        power_state(
            {
                "features": [
                    {"name": "power", "properties": [{**prop, "error": {"type": "X"}}]}
                ]
            }
        )
        is None
    )
    assert power_state(None) is None
    assert is_reachable({"features": []}) is None
    assert power_state(with_power_state({}, on=True)) is True


def test_is_thermostat_by_feature_only() -> None:
    """A thermostat feature is enough even with an unexpected category."""
    endpoint = {
        "displayCategories": {"primary": {"value": "OTHER"}},
        "features": [{"name": "thermostat"}],
    }
    assert is_thermostat(endpoint)
    assert not is_thermostat({"displayCategories": None, "features": None})


def test_categories_and_summary(endpoints: list[dict[str, Any]]) -> None:
    """Summary counts primary categories."""
    primary, all_cats = endpoint_categories(endpoints[0])
    assert primary == "THERMOSTAT"
    assert all_cats == {"THERMOSTAT", "TEMPERATURE_SENSOR"}
    assert summarize_endpoints([*endpoints, {}]) == {
        "LIGHT": 1,
        "SMARTPLUG": 2,
        "THERMOSTAT": 1,
        "UNKNOWN": 1,
    }


def test_parse_phoenix() -> None:
    """Phoenix JSON-in-JSON is flattened."""
    appliances = parse_phoenix(load_fixture("phoenix.json"))
    assert len(appliances) == 3
    assert [a["friendlyName"] for a in appliances if phoenix_is_thermostat(a)] == [
        "Hallway Thermostat"
    ]
    assert [a["friendlyName"] for a in appliances if phoenix_is_supported(a)] == [
        "Hallway Thermostat",
        "Coffee Maker",
    ]
    assert parse_phoenix({"networkDetail": "not json"}) == []
    assert parse_phoenix({}) == []


def _client(response: Any) -> AlexaSmartHomeClient:
    client = AlexaSmartHomeClient(MagicMock())
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]
    return client


async def test_get_states_rich_and_minimal() -> None:
    """The right query is sent and results are keyed by endpoint id."""
    client = _client(load_fixture("state.json"))
    states = await client.async_get_states(["a"])
    assert list(states) == [
        "amzn1.alexa.endpoint.11111111-2222-3333-4444-555555555555",
        "amzn1.alexa.endpoint.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    ]
    sent = client._request.call_args.args[2]
    assert sent["query"] == QUERY_STATE_RICH
    assert sent["variables"] == {"endpointIds": ["a"]}

    await client.async_get_states(["a"], rich=False)
    assert client._request.call_args.args[2]["query"] == QUERY_STATE_MINIMAL


async def test_get_states_empty_ids_skips_request() -> None:
    """No ids, no request."""
    client = _client({})
    assert await client.async_get_states([]) == {}
    client._request.assert_not_called()


async def test_get_states_graphql_error() -> None:
    """Errors without data raise; partial errors are tolerated."""
    errors = [{"message": "Unknown type Setpoint", "path": ["x"]}]
    with pytest.raises(AlexaGraphQLError) as exc:
        await _client({"errors": errors}).async_get_states(["a"])
    assert exc.value.errors == errors

    partial = load_fixture("state.json") | {"errors": errors}
    assert len(await _client(partial).async_get_states(["a"])) == 2


async def test_list_endpoints_error() -> None:
    """listEndpoints errors raise."""
    with pytest.raises(AlexaGraphQLError):
        await _client({"errors": [{"message": "boom"}]}).async_list_endpoints()


async def test_set_feature() -> None:
    """Mutation payload shape and error surfacing."""
    ok = {
        "data": {"setEndpointFeatures": {"featureControlResponses": [{}], "errors": []}}
    }
    client = _client(ok)
    await client.async_set_feature(
        "ep", "thermostat", "setThermostatMode", {"thermostatMode": "HEAT"}
    )
    request = client._request.call_args.args[2]["variables"]["featureControlRequests"][
        0
    ]
    assert request == {
        "endpointId": "ep",
        "featureName": "thermostat",
        "featureOperationName": "setThermostatMode",
        "payload": {"thermostatMode": "HEAT"},
    }

    bad = {
        "data": {
            "setEndpointFeatures": {
                "errors": [{"endpointId": "ep", "code": "DEVICE_OFFLINE"}]
            }
        }
    }
    with pytest.raises(AlexaGraphQLError, match="DEVICE_OFFLINE"):
        await _client(bad).async_set_feature("ep", "thermostat", "setThermostatMode")
