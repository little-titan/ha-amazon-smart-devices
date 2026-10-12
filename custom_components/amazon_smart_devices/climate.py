"""Climate platform: Amazon Smart Thermostat."""

from __future__ import annotations

from typing import Any

from aioamazondevices.exceptions import AmazonError
from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_ON,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from .api import (
    FAN_INSTANCE,
    MODE_FEATURE,
    THERMOSTAT_FEATURE,
    DeviceKind,
    allowed_range,
    auto_setpoints,
    current_temperature,
    fan_mode,
    hvac_stage,
    target_setpoint,
    thermostat_mode,
    with_auto_setpoints,
    with_fan_mode,
    with_target_setpoint,
    with_thermostat_mode,
)
from .const import DOMAIN
from .coordinator import SmartDevicesConfigEntry, SmartDevicesCoordinator
from .entity import AmazonSmartDeviceEntity

# Commands go to Amazon's cloud; send them one at a time.
PARALLEL_UPDATES = 1

# The four modes the thermostat supports. Auto keeps a low (heat) and a high
# (cool) setpoint instead of a single target.
ALEXA_TO_HVAC = {
    "OFF": HVACMode.OFF,
    "HEAT": HVACMode.HEAT,
    "COOL": HVACMode.COOL,
    "AUTO": HVACMode.HEAT_COOL,
}
HVAC_TO_ALEXA = {v: k for k, v in ALEXA_TO_HVAC.items()}

# Fan modes live on the `mode` feature, instance "5". Circulate runs the fan
# on a duty cycle, so it is exposed as a custom fan mode.
FAN_CIRCULATE = "circulate"
ALEXA_TO_FAN = {"1": FAN_ON, "2": FAN_AUTO, "3": FAN_CIRCULATE}
FAN_TO_ALEXA = {v: k for k, v in ALEXA_TO_FAN.items()}

SCALE_TO_UNIT = {
    "FAHRENHEIT": UnitOfTemperature.FAHRENHEIT,
    "CELSIUS": UnitOfTemperature.CELSIUS,
}
UNIT_TO_SCALE = {v: k for k, v in SCALE_TO_UNIT.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartDevicesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a climate entity per Amazon Smart Thermostat, including later ones."""
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [
            eid for eid in coordinator.ids_of(DeviceKind.THERMOSTAT) if eid not in added
        ]
        if new:
            added.update(new)
            async_add_entities(AmazonThermostat(coordinator, eid) for eid in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AmazonThermostat(AmazonSmartDeviceEntity, ClimateEntity, RestoreEntity):
    """An Amazon Smart Thermostat."""

    _attr_name = None  # the entity is the device
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_translation_key = "thermostat"
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: SmartDevicesCoordinator, endpoint_id: str) -> None:
        """Bind the entity to an endpoint."""
        super().__init__(coordinator, endpoint_id)
        self._attr_hvac_modes = list(HVAC_TO_ALEXA)
        self._attr_fan_modes = list(FAN_TO_ALEXA)
        # Alexa has one target setpoint shared by Heat and Cool, so the value
        # each mode last had is remembered here and re-applied on a mode switch.
        self._remembered: dict[str, float] = {}
        self._switching = False

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Persist the remembered setpoints across restarts."""
        return RestoredExtraData(dict(self._remembered))

    async def async_added_to_hass(self) -> None:
        """Restore the remembered setpoints, then learn from the current state."""
        await super().async_added_to_hass()
        if (extra := await self.async_get_last_extra_data()) is not None:
            self._remembered = {
                k: float(v) for k, v in extra.as_dict().items() if v is not None
            }
        self._remember_current()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_current()
        super()._handle_coordinator_update()

    def _remember_current(self) -> None:
        """Record the setpoints Alexa reports for the mode it is in."""
        if self._switching:
            return
        if (mode := thermostat_mode(self.endpoint_state)) in ("HEAT", "COOL"):
            if setpoint := target_setpoint(self.endpoint_state):
                self._remembered[mode.lower()] = setpoint[0]
        elif points := auto_setpoints(self.endpoint_state):
            self._remembered["heat"] = points[0][0]
            self._remembered["cool"] = points[1][0]

    @property
    def temperature_unit(self) -> str:
        """Return the unit Alexa reports in (Fahrenheit until proven otherwise)."""
        reading = current_temperature(self.endpoint_state) or target_setpoint(
            self.endpoint_state
        )
        return SCALE_TO_UNIT.get(
            reading[1] if reading else "", UnitOfTemperature.FAHRENHEIT
        )

    @property
    def current_temperature(self) -> float | None:
        """Return the measured temperature."""
        reading = current_temperature(self.endpoint_state)
        return reading[0] if reading else None

    @property
    def target_temperature(self) -> float | None:
        """Return the target setpoint (none in Auto, which has a low and a high)."""
        if self.hvac_mode is HVACMode.HEAT_COOL:
            return None
        setpoint = target_setpoint(self.endpoint_state)
        return setpoint[0] if setpoint else None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the Auto-mode heat setpoint."""
        points = auto_setpoints(self.endpoint_state)
        return points[0][0] if points else None

    @property
    def target_temperature_high(self) -> float | None:
        """Return the Auto-mode cool setpoint."""
        points = auto_setpoints(self.endpoint_state)
        return points[1][0] if points else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the thermostat mode."""
        return ALEXA_TO_HVAC.get(thermostat_mode(self.endpoint_state) or "")

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return what the equipment is doing right now."""
        if self.hvac_mode is HVACMode.OFF:
            return HVACAction.OFF
        cooling = hvac_stage(self.endpoint_state, "coolerStage")
        heating = hvac_stage(self.endpoint_state, "primaryHeaterStage")
        if cooling is None and heating is None:
            return None
        if cooling not in (None, "OFF"):
            return HVACAction.COOLING
        if heating not in (None, "OFF"):
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def min_temp(self) -> float:
        """Return the lowest setpoint the thermostat accepts in this mode."""
        return self._limit(0, super().min_temp)

    @property
    def max_temp(self) -> float:
        """Return the highest setpoint the thermostat accepts in this mode."""
        return self._limit(1, super().max_temp)

    def _limit(self, index: int, default: float) -> float:
        kind = "cooling" if self.hvac_mode is HVACMode.COOL else "heating"
        if (limits := allowed_range(self.endpoint_state, kind)) is None:
            return default
        return limits[index]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the thermostat mode."""
        mode = HVAC_TO_ALEXA[hvac_mode]
        self._remember_current()
        self._switching = True
        try:
            await self._async_command("setThermostatMode", {"thermostatMode": mode})
            state = with_thermostat_mode(self.endpoint_state or {}, mode)
            self.coordinator.async_set_optimistic(self.endpoint_id, state)
            await self._async_restore_setpoint(mode, state)
        finally:
            self._switching = False
        if hvac_mode is HVACMode.HEAT_COOL:
            # The low/high setpoints only exist once Alexa is in Auto, so the
            # optimistic state cannot supply them; fetch them in the background.
            self.hass.async_create_task(self.coordinator.async_request_refresh())
        if hvac_mode is HVACMode.HEAT_COOL:
            # The low/high setpoints only exist once Alexa is in Auto, so the
            # optimistic state cannot supply them; fetch them in the background.
            self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def _async_restore_setpoint(self, mode: str, state: dict[str, Any]) -> None:
        """Re-apply the setpoint this mode had last time, if it differs."""
        remembered = self._remembered.get(mode.lower())
        current = target_setpoint(state)
        if mode not in ("HEAT", "COOL") or remembered is None or current is None:
            return
        if remembered == current[0]:
            return
        scale = current[1]
        await self._async_command(
            "setTargetSetpoint",
            {"targetSetpoint": {"value": remembered, "scale": scale}},
        )
        self.coordinator.async_set_optimistic(
            self.endpoint_id, with_target_setpoint(state, remembered, scale)
        )

    @property
    def fan_mode(self) -> str | None:
        """Return the fan mode."""
        return ALEXA_TO_FAN.get(fan_mode(self.endpoint_state) or "")

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        mode = FAN_TO_ALEXA[fan_mode]
        await self._async_command(
            "setMode", {"mode": mode}, MODE_FEATURE, instance=FAN_INSTANCE
        )
        self.coordinator.async_set_optimistic(
            self.endpoint_id, with_fan_mode(self.endpoint_state or {}, mode)
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        scale = UNIT_TO_SCALE[UnitOfTemperature(self.temperature_unit)]
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if low is not None and high is not None:
            await self._async_command(
                "setTargetSetpoint",
                {
                    "lowerSetpoint": {"value": low, "scale": scale},
                    "upperSetpoint": {"value": high, "scale": scale},
                },
            )
            self.coordinator.async_set_optimistic(
                self.endpoint_id,
                with_auto_setpoints(self.endpoint_state or {}, low, high, scale),
            )
            return
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self._async_command(
            "setTargetSetpoint",
            {"targetSetpoint": {"value": temperature, "scale": scale}},
        )
        self.coordinator.async_set_optimistic(
            self.endpoint_id,
            with_target_setpoint(self.endpoint_state or {}, temperature, scale),
        )

    async def _async_command(
        self,
        operation: str,
        payload: dict[str, Any],
        feature: str = THERMOSTAT_FEATURE,
        instance: str | None = None,
    ) -> None:
        try:
            await self.coordinator.client.async_set_feature(
                self.endpoint_id,
                feature,
                operation,
                payload,
                **({"instance": instance} if instance else {}),
            )
        except (AmazonError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
