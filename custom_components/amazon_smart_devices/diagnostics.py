"""Diagnostics for Amazon Smart Devices.

This is also the Milestone-1 capture path for people running Home Assistant
OS: install the integration, then Settings -> Devices & services -> Amazon
Smart Devices -> ... -> Download diagnostics. The file contains the raw
GraphQL metadata and live state of every supported device, identifiers
redacted.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import parse_phoenix, phoenix_is_supported
from .const import CONF_LOGIN_DATA
from .coordinator import SmartDevicesConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_LOGIN_DATA,
    "serialNumber",
    "endpointId",
    "entityId",
    "applianceId",
    "id",
    "customerId",
    "deviceOwnerCustomerId",
    "macAddress",
    "ssid",
    "title",
    "unique_id",
}


def _lib_version() -> str:
    try:
        return version("aioamazondevices")
    except PackageNotFoundError:
        return "unknown"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SmartDevicesConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    phoenix: Any
    try:
        phoenix = [
            a
            for a in parse_phoenix(await coordinator.client.async_get_phoenix())
            if phoenix_is_supported(a)
        ]
    except Exception as err:  # noqa: BLE001 - diagnostics must not fail
        phoenix = {"error": repr(err)}

    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "aioamazondevices_version": _lib_version(),
            "rich_state_supported": coordinator.rich_state_supported,
            "last_update_success": coordinator.last_update_success,
            "endpoint_summary": coordinator.endpoint_summary,
            # Lists, not dicts keyed by endpoint id, so ids get redacted.
            "device_kinds": sorted(coordinator.kinds.values()),
            "device_endpoints": list(coordinator.devices.values()),
            "device_state": list((coordinator.data or {}).values()),
            "phoenix_devices": phoenix,
        },
        TO_REDACT,
    )
