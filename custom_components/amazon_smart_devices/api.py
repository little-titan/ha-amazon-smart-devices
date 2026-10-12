"""Thin Alexa Smart Home client for Amazon devices that only work via Alexa.

This module deliberately has NO Home Assistant imports so that:

* ``scripts/discover.py`` can load it standalone (Milestone 1), and
* it maps 1:1 onto a future ``aioamazondevices`` handler when this work is
  upstreamed (see README, "Upstreaming").

Authentication, cookies, CSRF and retries are all delegated to
``aioamazondevices.AmazonEchoApi``. ``aioamazondevices`` only builds devices for
Echo hardware (devices-v2 entries with a MICROPHONE capability, plus the AQM);
every other Alexa Smart Home endpoint -- including the Amazon Smart
Thermostat and Amazon Smart Plug -- is dropped in
``AmazonDeviceHandler.update_devices``. We
therefore issue our own GraphQL queries through the library's authenticated
HTTP wrapper.

The wrapper is a private attribute (``AmazonEchoApi._http_wrapper``). Its
shape has been stable across the 16.x series; all access to it is confined to
``_request`` below so a library change breaks exactly one function.
"""

from __future__ import annotations

import copy
from enum import StrEnum
from http import HTTPMethod
import json
import logging
from typing import TYPE_CHECKING, Any

from aioamazondevices.const.http import REQUEST_AGENT, URI_NEXUS_GRAPHQL
from aioamazondevices.exceptions import CannotRetrieveData
from yarl import URL

if TYPE_CHECKING:
    from aioamazondevices.api import AmazonEchoApi

_LOGGER = logging.getLogger(__name__)

ENDPOINT_ID_PREFIX = "amzn1.alexa.endpoint."
THERMOSTAT_CATEGORY = "THERMOSTAT"
THERMOSTAT_FEATURE = "thermostat"
SMART_PLUG_CATEGORY = "SMARTPLUG"
POWER_FEATURE = "power"
MODE_FEATURE = "mode"
FAN_INSTANCE = "5"
AMAZON_MANUFACTURER = "amazon"


class DeviceKind(StrEnum):
    """Device types this integration supports."""

    THERMOSTAT = "thermostat"
    SMART_PLUG = "smart_plug"


# ---------------------------------------------------------------------------
# GraphQL documents
#
# Every field and fragment type below is already used in production either by
# aioamazondevices (listEndpoints, Reachability, TemperatureSensor,
# RangeValue, error/accuracy/timeOfSample) or by homebridge-alexa-smarthome /
# ha-alexa-smarthome (Setpoint, ThermostatMode, ToggleState, Power,
# operations, RangeConfiguration). An unknown fragment type makes the whole
# query fail, so nothing speculative goes in the rich query; ``__typename``
# on every property is what reveals anything we are not yet mapping.
# ---------------------------------------------------------------------------

QUERY_LIST_ENDPOINTS = """
query listAllEndpoints {
  listEndpoints(listEndpointsInput: { includeHouseholdDevices: true }) {
    endpoints {
      endpointId: id
      friendlyNameObject { value { text } }
      manufacturer { value { text } }
      model { value { text } }
      serialNumber { value { text } }
      softwareVersion { value { text } }
      enablement
      displayCategories {
        all { value }
        primary { value }
      }
      alexaEnabledMetadata { category capabilities }
      legacyIdentifiers {
        dmsIdentifier { deviceType { value { text } } }
        chrsIdentifier { entityId }
      }
      legacyAppliance { applianceId }
      features {
        name
        instance
        operations { name }
        properties { name type __typename }
      }
    }
  }
}
"""

_STATE_FEATURES_RICH = """
  features {
    name
    instance
    properties {
      name
      type
      accuracy
      error { type message }
      __typename
      ... on TemperatureSensor {
        value { value scale }
        timeOfSample
        timeOfLastChange
      }
      ... on Setpoint {
        value { value scale }
      }
      ... on ThermostatMode {
        thermostatModeValue
      }
      ... on ThermostatConfigurationAllowedTemperatureRange {
        thermostatAllowedTemperatureRangeValue {
          heating { minimum { value scale } maximum { value scale } }
          cooling { minimum { value scale } maximum { value scale } }
        }
      }
      ... on Mode {
        modeValue { value }
      }
      ... on HvacCoolerStage {
        coolerStageValue { value }
      }
      ... on HvacPrimaryHeaterStage {
        primaryHeaterStageValue { value }
      }
      ... on RangeValue {
        rangeValue { value }
        timeOfSample
        timeOfLastChange
      }
      ... on ToggleState {
        toggleStateValue
      }
      ... on Power {
        powerStateValue
      }
      ... on Reachability {
        reachabilityStatusValue
        timeOfSample
        timeOfLastChange
      }
    }
    configuration {
      ... on RangeConfiguration {
        friendlyName { value { text } }
      }
    }
  }
"""

_STATE_FEATURES_MINIMAL = """
  features {
    name
    instance
    properties { name type __typename error { type message } }
  }
"""

_QUERY_STATE_TEMPLATE = """
query getEndpointStates($endpointIds: [String]!) {
  listEndpoints(
    listEndpointsInput: {
      latencyTolerance: LOW,
      endpointIds: $endpointIds,
      includeHouseholdDevices: true
    }
  ) {
    endpoints {
      endpointId: id
      friendlyNameObject { value { text } }
      %s
    }
  }
}
"""

QUERY_STATE_RICH = _QUERY_STATE_TEMPLATE % _STATE_FEATURES_RICH
QUERY_STATE_MINIMAL = _QUERY_STATE_TEMPLATE % _STATE_FEATURES_MINIMAL

# Write path (used from Milestone 3). Same mutation the Alexa web app and
# ha-alexa-smarthome use.
MUTATION_SET_FEATURES = """
mutation setEndpointFeatures(
  $featureControlRequests: [FeatureControlRequest!]!
) {
  setEndpointFeatures(
    setEndpointFeaturesInput: { featureControlRequests: $featureControlRequests }
  ) {
    featureControlResponses { endpointId featureOperationName }
    errors { endpointId code }
  }
}
"""


class AlexaGraphQLError(CannotRetrieveData):
    """GraphQL answered 200 but reported errors."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        """Keep the raw errors for diagnostics."""
        self.errors = errors
        first = errors[0] if errors else {}
        super().__init__(
            f"GraphQL error: {first.get('message', 'unknown')} "
            f"(path={first.get('path')})"
        )


# ---------------------------------------------------------------------------
# Pure helpers (unit tested, no I/O)
# ---------------------------------------------------------------------------


def endpoint_text(endpoint: dict[str, Any], key: str) -> str | None:
    """Return ``endpoint[key].value.text`` or None."""
    return ((endpoint.get(key) or {}).get("value") or {}).get("text")


def endpoint_categories(endpoint: dict[str, Any]) -> tuple[str | None, set[str]]:
    """Return (primary category, all categories) of an endpoint."""
    cats = endpoint.get("displayCategories") or {}
    primary = (cats.get("primary") or {}).get("value")
    all_values = {c.get("value") for c in (cats.get("all") or []) if c.get("value")}
    if primary:
        all_values.add(primary)
    return primary, all_values


def is_thermostat(endpoint: dict[str, Any]) -> bool:
    """Return True if an endpoint looks like a thermostat.

    Checked in order of reliability: the display category Alexa uses to
    render the device, then the presence of a ``thermostat`` feature (covers
    endpoints whose primary category is something else, e.g. a combined
    thermostat/sensor endpoint).
    """
    _, categories = endpoint_categories(endpoint)
    if THERMOSTAT_CATEGORY in categories:
        return True
    return any(
        (f.get("name") or "") == THERMOSTAT_FEATURE
        for f in endpoint.get("features") or []
    )


def is_amazon_made(endpoint: dict[str, Any]) -> bool:
    """Return True if Alexa reports Amazon as the manufacturer."""
    manufacturer = endpoint_text(endpoint, "manufacturer") or ""
    return AMAZON_MANUFACTURER in manufacturer.lower()


def has_feature(endpoint: dict[str, Any], name: str) -> bool:
    """Return True if the endpoint exposes a feature with this name."""
    return any((f.get("name") or "") == name for f in endpoint.get("features") or [])


def is_amazon_smart_plug(endpoint: dict[str, Any]) -> bool:
    """Return True for an Amazon-made smart plug.

    Third-party plugs (Kasa, Wemo, ...) are skipped on purpose: they have
    native Home Assistant integrations that are local and push-based.
    """
    _, categories = endpoint_categories(endpoint)
    return (
        SMART_PLUG_CATEGORY in categories
        and is_amazon_made(endpoint)
        and has_feature(endpoint, POWER_FEATURE)
    )


def device_kind(endpoint: dict[str, Any]) -> DeviceKind | None:
    """Return the supported device type of an endpoint, or None."""
    if is_thermostat(endpoint):
        return DeviceKind.THERMOSTAT
    if is_amazon_smart_plug(endpoint):
        return DeviceKind.SMART_PLUG
    return None


def _properties(state: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Yield (feature name, property) pairs from a state endpoint."""
    out: list[tuple[str, dict[str, Any]]] = []
    for feature in (state or {}).get("features") or []:
        for prop in feature.get("properties") or []:
            out.append((feature.get("name") or "", prop))
    return out


def power_state(state: dict[str, Any] | None) -> bool | None:
    """Return True/False for ON/OFF, None if unknown or errored."""
    for feature, prop in _properties(state):
        if feature == POWER_FEATURE and "powerStateValue" in prop:
            if prop.get("error"):
                return None
            value = (prop.get("powerStateValue") or "").upper()
            return {"ON": True, "OFF": False}.get(value)
    return None


def _thermostat_property(
    state: dict[str, Any] | None, feature_name: str, prop_name: str
) -> dict[str, Any] | None:
    """Return a healthy property of a feature, None if absent or errored."""
    for feature, prop in _properties(state):
        if (
            feature == feature_name
            and prop.get("name") == prop_name
            and not prop.get("error")
        ):
            return prop
    return None


def _temperature(prop: dict[str, Any] | None) -> tuple[float, str] | None:
    value = (prop or {}).get("value") or {}
    if value.get("value") is None:
        return None
    return float(value["value"]), str(value.get("scale") or "").upper()


def current_temperature(state: dict[str, Any] | None) -> tuple[float, str] | None:
    """Return (value, scale) measured by the thermostat, None if unknown."""
    return _temperature(_thermostat_property(state, "temperatureSensor", "temperature"))


def target_setpoint(state: dict[str, Any] | None) -> tuple[float, str] | None:
    """Return (value, scale) of the single target setpoint, None if unknown."""
    return _temperature(
        _thermostat_property(state, THERMOSTAT_FEATURE, "targetSetpoint")
    )


def auto_setpoints(
    state: dict[str, Any] | None,
) -> tuple[tuple[float, str], tuple[float, str]] | None:
    """Return ((low, scale), (high, scale)) while in Auto mode, else None."""
    low = _temperature(_thermostat_property(state, THERMOSTAT_FEATURE, "lowerSetpoint"))
    high = _temperature(
        _thermostat_property(state, THERMOSTAT_FEATURE, "upperSetpoint")
    )
    return (low, high) if low and high else None


def thermostat_mode(state: dict[str, Any] | None) -> str | None:
    """Return the Alexa thermostat mode (HEAT, COOL, OFF ...), None if unknown."""
    prop = _thermostat_property(state, THERMOSTAT_FEATURE, "thermostatMode")
    mode = (prop or {}).get("thermostatModeValue")
    return str(mode).upper() if mode else None


def hvac_stage(state: dict[str, Any] | None, key: str) -> str | None:
    """Return a stage value (``coolerStage`` / ``primaryHeaterStage``), if any."""
    for feature, prop in _properties(state):
        if feature == "hvac" and f"{key}Value" in prop and not prop.get("error"):
            value = (prop.get(f"{key}Value") or {}).get("value")
            return str(value).upper() if value else None
    return None


def allowed_range(
    state: dict[str, Any] | None, kind: str
) -> tuple[float, float] | None:
    """Return (min, max) allowed for ``heating`` or ``cooling``, if reported."""
    for _, prop in _properties(state):
        rng = (prop.get("thermostatAllowedTemperatureRangeValue") or {}).get(kind)
        if rng and rng.get("minimum") and rng.get("maximum"):
            return float(rng["minimum"]["value"]), float(rng["maximum"]["value"])
    return None


def _set_thermostat_property(
    state: dict[str, Any], prop_name: str, **fields: Any
) -> dict[str, Any]:
    """Return a copy of ``state`` with fields set on one thermostat property."""
    new = copy.deepcopy(state)
    for feature in new.get("features") or []:
        if feature.get("name") != THERMOSTAT_FEATURE:
            continue
        for prop in feature.get("properties") or []:
            if prop.get("name") == prop_name:
                prop.update(fields, error=None)
                return new
        feature.setdefault("properties", []).append({"name": prop_name, **fields})
        return new
    new.setdefault("features", []).append(
        {
            "name": THERMOSTAT_FEATURE,
            "properties": [{"name": prop_name, **fields}],
        }
    )
    return new


def with_thermostat_mode(state: dict[str, Any], mode: str) -> dict[str, Any]:
    """Return a copy of ``state`` with the thermostat mode set (optimistic)."""
    return _set_thermostat_property(state, "thermostatMode", thermostatModeValue=mode)


def with_target_setpoint(
    state: dict[str, Any], value: float, scale: str
) -> dict[str, Any]:
    """Return a copy of ``state`` with the target setpoint set (optimistic)."""
    return _set_thermostat_property(
        state, "targetSetpoint", value={"value": value, "scale": scale}
    )


def with_auto_setpoints(
    state: dict[str, Any], low: float, high: float, scale: str
) -> dict[str, Any]:
    """Return a copy of ``state`` with the Auto low/high set (optimistic)."""
    new = _set_thermostat_property(
        state, "lowerSetpoint", value={"value": low, "scale": scale}
    )
    return _set_thermostat_property(
        new, "upperSetpoint", value={"value": high, "scale": scale}
    )


def fan_mode(state: dict[str, Any] | None) -> str | None:
    """Return the Alexa fan mode id ("1" on, "2" auto, "3" circulate), if known."""
    for feature in (state or {}).get("features") or []:
        if (
            feature.get("name") != MODE_FEATURE
            or feature.get("instance") != FAN_INSTANCE
        ):
            continue
        for prop in feature.get("properties") or []:
            value = (prop.get("modeValue") or {}).get("value")
            if value and not prop.get("error"):
                return str(value)
    return None


def with_fan_mode(state: dict[str, Any], mode: str) -> dict[str, Any]:
    """Return a copy of ``state`` with the fan mode set (optimistic)."""
    new = copy.deepcopy(state)
    features = new.setdefault("features", [])
    for feature in features:
        if (
            feature.get("name") == MODE_FEATURE
            and feature.get("instance") == FAN_INSTANCE
        ):
            feature["properties"] = [
                {"name": "mode", "__typename": "Mode", "modeValue": {"value": mode}}
            ]
            return new
    features.append(
        {
            "name": MODE_FEATURE,
            "instance": FAN_INSTANCE,
            "properties": [
                {"name": "mode", "__typename": "Mode", "modeValue": {"value": mode}}
            ],
        }
    )
    return new


def is_reachable(state: dict[str, Any] | None) -> bool | None:
    """Return the Alexa reachability of an endpoint, None if not reported."""
    for _, prop in _properties(state):
        if "reachabilityStatusValue" in prop:
            return (prop.get("reachabilityStatusValue") or "").upper() == "OK"
    return None


def with_power_state(state: dict[str, Any], on: bool) -> dict[str, Any]:
    """Return a copy of ``state`` with its power property set (optimistic)."""
    new = copy.deepcopy(state)
    value = "ON" if on else "OFF"
    for feature in new.get("features") or []:
        if feature.get("name") != POWER_FEATURE:
            continue
        for prop in feature.get("properties") or []:
            if "powerStateValue" in prop:
                prop["powerStateValue"] = value
                prop["error"] = None
                return new
    new.setdefault("features", []).append(
        {
            "name": POWER_FEATURE,
            "properties": [{"name": "powerState", "powerStateValue": value}],
        }
    )
    return new


def summarize_endpoints(endpoints: list[dict[str, Any]]) -> dict[str, int]:
    """Count endpoints per primary category (for errors and diagnostics)."""
    summary: dict[str, int] = {}
    for endpoint in endpoints:
        primary, _ = endpoint_categories(endpoint)
        key = primary or "UNKNOWN"
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def parse_phoenix(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the /api/phoenix ``networkDetail`` JSON-in-JSON to appliances."""
    detail: Any = raw.get("networkDetail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return []
    appliances: list[dict[str, Any]] = []
    for location in ((detail or {}).get("locationDetails") or {}).values():
        bridges = (location.get("amazonBridgeDetails") or {}).get(
            "amazonBridgeDetails"
        ) or {}
        for bridge in bridges.values():
            details = (bridge.get("applianceDetails") or {}).get(
                "applianceDetails"
            ) or {}
            appliances.extend(details.values())
    return appliances


def phoenix_is_thermostat(appliance: dict[str, Any]) -> bool:
    """Return True if a phoenix appliance is a thermostat."""
    if THERMOSTAT_CATEGORY in (appliance.get("applianceTypes") or []):
        return True
    return any(
        "ThermostatController" in (cap.get("interfaceName") or "")
        for cap in appliance.get("capabilities") or []
    )


def phoenix_is_supported(appliance: dict[str, Any]) -> bool:
    """Return True if a phoenix appliance is a supported device type."""
    if phoenix_is_thermostat(appliance):
        return True
    return (
        SMART_PLUG_CATEGORY in (appliance.get("applianceTypes") or [])
        and AMAZON_MANUFACTURER in (appliance.get("manufacturerName") or "").lower()
    )


def _graphql_errors(response: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(response, list):
        response = response[0] if response else {}
    return list(response.get("errors") or [])


def _graphql_endpoints(response: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(response, list):
        response = response[0] if response else {}
    data = response.get("data") or {}
    return list(((data.get("listEndpoints") or {}).get("endpoints")) or [])


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AlexaSmartHomeClient:
    """Reads and controls Alexa Smart Home endpoints."""

    def __init__(self, api: AmazonEchoApi) -> None:
        """Wrap an already-constructed AmazonEchoApi."""
        self._api = api

    @property
    def api(self) -> AmazonEchoApi:
        """Return the underlying AmazonEchoApi."""
        return self._api

    async def _request(
        self, method: str, path: str, payload: Any | None = None
    ) -> dict[str, Any]:
        """Authenticated request to the Alexa website API, returns JSON.

        The ONLY place that touches aioamazondevices private attributes.
        """
        wrapper = self._api._http_wrapper  # noqa: SLF001
        state = self._api._session_state_data  # noqa: SLF001
        _, resp = await wrapper.session_request(
            method=method,
            url=URL.joinpath(state.alexa_website_url, path),
            input_data=payload,
            json_data=payload is not None,
            extended_headers={"User-Agent": REQUEST_AGENT["Amazon"]},
        )
        # description="" keeps the library from debug-logging full payloads
        return await wrapper.response_to_json(resp, "")

    async def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        return await self._request(HTTPMethod.POST, URI_NEXUS_GRAPHQL, payload)

    async def async_list_endpoints(self) -> list[dict[str, Any]]:
        """Return every Alexa endpoint on the account (raw GraphQL)."""
        response = await self._graphql(QUERY_LIST_ENDPOINTS)
        endpoints = _graphql_endpoints(response)
        if not endpoints and (errors := _graphql_errors(response)):
            raise AlexaGraphQLError(errors)
        return endpoints

    async def async_get_states(
        self, endpoint_ids: list[str], *, rich: bool = True
    ) -> dict[str, dict[str, Any]]:
        """Return raw feature state keyed by endpoint id.

        With ``rich=True`` partial GraphQL errors (one property failing) are
        tolerated as long as endpoints come back; a response with no
        endpoints and errors raises AlexaGraphQLError.
        """
        if not endpoint_ids:
            return {}
        response = await self._graphql(
            QUERY_STATE_RICH if rich else QUERY_STATE_MINIMAL,
            {"endpointIds": endpoint_ids},
        )
        endpoints = _graphql_endpoints(response)
        errors = _graphql_errors(response)
        if not endpoints and errors:
            raise AlexaGraphQLError(errors)
        if errors:
            _LOGGER.debug("Partial GraphQL errors in state query: %s", errors)
        return {e["endpointId"]: e for e in endpoints if e.get("endpointId")}

    async def async_get_phoenix(self) -> dict[str, Any]:
        """Return the legacy /api/phoenix appliance tree (diagnostics only).

        It carries capability *configuration* (supported modes, setpoint
        limits) that the GraphQL property query does not.
        """
        return await self._request(HTTPMethod.GET, "api/phoenix")

    async def async_set_feature(
        self,
        endpoint_id: str,
        feature_name: str,
        operation_name: str,
        payload: dict[str, Any] | None = None,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a feature operation (e.g. thermostat/setThermostatMode)."""
        request: dict[str, Any] = {
            "endpointId": endpoint_id,
            "featureName": feature_name,
            "featureOperationName": operation_name,
        }
        if instance:
            request["instance"] = instance
        if payload:
            request["payload"] = payload
        response = await self._graphql(
            MUTATION_SET_FEATURES, {"featureControlRequests": [request]}
        )
        if errors := _graphql_errors(response):
            raise AlexaGraphQLError(errors)
        result = (response.get("data") or {}).get("setEndpointFeatures") or {}
        if result.get("errors"):
            raise AlexaGraphQLError(
                [
                    {"message": e.get("code"), "path": e.get("endpointId")}
                    for e in result["errors"]
                ]
            )
        return result
