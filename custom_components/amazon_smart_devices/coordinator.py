"""Data update coordinator for Amazon Smart Devices."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRetrieveData,
)
from aioamazondevices.structures import AmazonSaveDataConfig
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AlexaGraphQLError,
    AlexaSmartHomeClient,
    DeviceKind,
    device_kind,
    endpoint_text,
    summarize_endpoints,
)
from .const import CONF_LOGIN_DATA, DISCOVERY_INTERVAL, DOMAIN, LOGGER, SCAN_INTERVAL

type SmartDevicesConfigEntry = ConfigEntry[SmartDevicesCoordinator]


class SmartDevicesCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls raw Alexa feature state for every supported device.

    ``data`` maps endpoint id -> raw GraphQL endpoint state. Parsing lives in
    ``api.py`` helpers used by the entities, so the raw payload stays
    available to diagnostics unchanged.
    """

    config_entry: SmartDevicesConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SmartDevicesConfigEntry,
        session: ClientSession,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=SCAN_INTERVAL,
        )
        api = AmazonEchoApi(
            session,
            entry.data[CONF_USERNAME],
            # The password is only used for interactive login; stored-data
            # login uses the tokens in CONF_LOGIN_DATA, so we never persist it.
            "",
            login_data=entry.data[CONF_LOGIN_DATA],
            save_data=AmazonSaveDataConfig(path=Path(hass.config.path(DOMAIN))),
        )
        self.client = AlexaSmartHomeClient(api)
        # endpoint id -> listEndpoints metadata (name, model, features ...)
        self.devices: dict[str, dict[str, Any]] = {}
        self.kinds: dict[str, DeviceKind] = {}
        self.endpoint_summary: dict[str, int] = {}
        self.rich_state_supported = True
        self._last_discovery: datetime | None = None

    async def _async_setup(self) -> None:
        """Log in with stored tokens and discover devices (first refresh)."""
        try:
            await self.client.api.login.login_mode_stored_data()
        except CannotAuthenticate as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except (CannotConnect, CannotRetrieveData) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": repr(err)},
            ) from err
        await self._async_discover()

    async def _async_discover(self) -> None:
        """(Re)read the endpoint list and register supported devices."""
        try:
            endpoints = await self.client.async_list_endpoints()
        except CannotAuthenticate as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except (CannotConnect, CannotRetrieveData, ValueError) as err:
            if self.devices:
                # Keep the known list; the device set changes rarely.
                LOGGER.warning("Device re-discovery failed: %r", err)
                return
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": repr(err)},
            ) from err

        self.endpoint_summary = summarize_endpoints(endpoints)
        devices: dict[str, dict[str, Any]] = {}
        kinds: dict[str, DeviceKind] = {}
        for endpoint in endpoints:
            endpoint_id = endpoint.get("endpointId")
            if endpoint_id and (kind := device_kind(endpoint)):
                devices[endpoint_id] = endpoint
                kinds[endpoint_id] = kind
        self.devices, self.kinds = devices, kinds
        self._last_discovery = dt_util.utcnow()
        if not self.devices:
            LOGGER.warning(
                "No supported device among %d Alexa endpoints (categories: %s)",
                len(endpoints),
                self.endpoint_summary,
            )
        self._register_devices()

    def _register_devices(self) -> None:
        """Create/refresh a device registry entry per supported device.

        Done here (not only from entities) so a device type without an entity
        platform yet (the thermostat until Milestone 3) still shows up under
        Settings -> Devices.
        """
        registry = dr.async_get(self.hass)
        for endpoint_id, endpoint in self.devices.items():
            registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id,
                identifiers={(DOMAIN, endpoint_id)},
                manufacturer=endpoint_text(endpoint, "manufacturer") or "Amazon",
                model=endpoint_text(endpoint, "model"),
                name=endpoint_text(endpoint, "friendlyNameObject")
                or self.kinds[endpoint_id].replace("_", " ").title(),
                serial_number=endpoint_text(endpoint, "serialNumber"),
                sw_version=endpoint_text(endpoint, "softwareVersion"),
            )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch raw state for all supported devices."""
        if (
            self._last_discovery is None
            or dt_util.utcnow() - self._last_discovery > DISCOVERY_INTERVAL
        ):
            await self._async_discover()

        ids = list(self.devices)
        try:
            return await self.client.async_get_states(
                ids, rich=self.rich_state_supported
            )
        except AlexaGraphQLError as err:
            if not self.rich_state_supported:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="cannot_retrieve_data",
                    translation_placeholders={"error": repr(err)},
                ) from err
            # A fragment type the account's schema does not know makes the
            # whole query fail. Fall back so diagnostics still show what the
            # endpoint exposes (values will be missing until fixed).
            LOGGER.warning(
                "Rich state query rejected (%s); falling back to "
                "the minimal query. Please attach diagnostics to an issue",
                err.errors,
            )
            self.rich_state_supported = False
            return await self._async_update_data()
        except CannotAuthenticate as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except (CannotConnect, CannotRetrieveData, ValueError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_retrieve_data",
                translation_placeholders={"error": repr(err)},
            ) from err

    def ids_of(self, kind: DeviceKind) -> list[str]:
        """Return endpoint ids of one device type."""
        return [eid for eid, k in self.kinds.items() if k is kind]

    def async_set_optimistic(self, endpoint_id: str, state: dict[str, Any]) -> None:
        """Replace one device's state after a successful command.

        Alexa's state cache can lag a command by a few seconds, so an
        immediate re-poll may return the old value; the next scheduled poll
        reconciles.
        """
        self.async_set_updated_data({**(self.data or {}), endpoint_id: state})
