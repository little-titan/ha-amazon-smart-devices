"""Base entity for Amazon Smart Devices."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import is_reachable
from .const import DOMAIN
from .coordinator import SmartDevicesCoordinator


class AmazonSmartDeviceEntity(CoordinatorEntity[SmartDevicesCoordinator]):
    """An entity backed by one Alexa endpoint.

    The device registry entry (name, model, serial ...) is created by the
    coordinator; entities only link to it by identifier.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartDevicesCoordinator, endpoint_id: str) -> None:
        """Bind the entity to an endpoint."""
        super().__init__(coordinator)
        self.endpoint_id = endpoint_id
        self._attr_unique_id = endpoint_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, endpoint_id)})

    @property
    def endpoint_state(self) -> dict[str, Any] | None:
        """Return the raw GraphQL state of this endpoint."""
        return (self.coordinator.data or {}).get(self.endpoint_id)

    @property
    def available(self) -> bool:
        """Available when polled successfully and Alexa can reach the device."""
        return (
            super().available
            and self.endpoint_state is not None
            and is_reachable(self.endpoint_state) is not False
        )
