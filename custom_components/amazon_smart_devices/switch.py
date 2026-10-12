"""Switch platform: Amazon Smart Plug."""

from __future__ import annotations

from typing import Any

from aioamazondevices.exceptions import AmazonError
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import POWER_FEATURE, DeviceKind, power_state, with_power_state
from .const import DOMAIN
from .coordinator import SmartDevicesConfigEntry
from .entity import AmazonSmartDeviceEntity

# Commands go to Amazon's cloud; send them one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartDevicesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a switch per Amazon Smart Plug, including plugs found later."""
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [
            eid for eid in coordinator.ids_of(DeviceKind.SMART_PLUG) if eid not in added
        ]
        if new:
            added.update(new)
            async_add_entities(AmazonSmartPlugSwitch(coordinator, eid) for eid in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AmazonSmartPlugSwitch(AmazonSmartDeviceEntity, SwitchEntity):
    """The on/off relay of an Amazon Smart Plug."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = None  # the entity is the device

    @property
    def is_on(self) -> bool | None:
        """Return the relay state reported by Alexa."""
        return power_state(self.endpoint_state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the plug on."""
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the plug off."""
        await self._async_set_power(False)

    async def _async_set_power(self, on: bool) -> None:
        try:
            await self.coordinator.client.async_set_feature(
                self.endpoint_id, POWER_FEATURE, "turnOn" if on else "turnOff"
            )
        except (AmazonError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_optimistic(
            self.endpoint_id, with_power_state(self.endpoint_state or {}, on)
        )
