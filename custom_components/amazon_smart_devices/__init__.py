"""The Amazon Smart Devices integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, device_registry as dr

from .const import DOMAIN
from .coordinator import SmartDevicesConfigEntry, SmartDevicesCoordinator

# Amazon Smart Plug -> switch, Amazon Smart Thermostat -> climate.
PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SWITCH]


async def async_setup_entry(
    hass: HomeAssistant, entry: SmartDevicesConfigEntry
) -> bool:
    """Set up Amazon Smart Devices from a config entry."""
    # A dedicated session: aioamazondevices keeps Amazon cookies in the
    # session's cookie jar, which must not be shared with other integrations.
    session = aiohttp_client.async_create_clientsession(hass)
    coordinator = SmartDevicesCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SmartDevicesConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SmartDevicesConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device that is no longer on the Alexa account."""
    return not any(
        identifier
        for identifier in device.identifiers
        if identifier[0] == DOMAIN and identifier[1] in entry.runtime_data.devices
    )
