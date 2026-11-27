"""Sensor platform for pypowerwall."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import PypowerwallConfigEntry
from .const import (
    CONF_CONN_TYPE,
    CONF_DESIGN_CAPACITY,
    DEFAULT_DESIGN_CAPACITY_KWH,
    TARIFF_CONN_TYPES,
)
from .coordinator import FAN_SIGNALS, PowerwallData, PowerwallDataUpdateCoordinator
from .entity import PowerwallEntity


@dataclass(frozen=True, kw_only=True)
class PowerwallSensorDescription(SensorEntityDescription):
    """Describes a pypowerwall sensor backed by a PowerwallData field."""

    value_fn: Callable[[PowerwallData], object]
    attributes_fn: Callable[[PowerwallData], dict[str, object]] | None = None


_GO_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


def _uptime_seconds(data: PowerwallData) -> int | None:
    """Gateway uptime in whole seconds.

    pypowerwall's uptime() is a Go-style duration string ("62h48m24.07s"), or a
    plain number of seconds on some paths. A numeric value with a unit keeps the
    sensor out of the logbook, which logs every state change of a text sensor
    (and this one changes on every poll).
    """
    raw = data.uptime
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    match = _GO_DURATION.match(text)
    if not text or match is None:
        return None
    hours, minutes, seconds = match.groups()
    return int(int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0))


def _battery_import_power(data: PowerwallData) -> float | None:
    """Power flowing into the battery (charging) as a positive value, else 0."""
    if data.battery_power is None:
        return None
    return max(-data.battery_power, 0)


def _battery_export_power(data: PowerwallData) -> float | None:
    """Power flowing out of the battery (discharging) as a positive value, else 0."""
    if data.battery_power is None:
        return None
    return max(data.battery_power, 0)


def _grid_import_power(data: PowerwallData) -> float | None:
    """Power drawn from the grid as a positive value, else 0.

    Opposite polarity from battery: pypowerwall's own /api/meters/aggregates
    glossary defines the site meter's energy_imported as "kWh pulled from
    grid" and energy_exported as "kWh pushed to grid" (README.md's Example API
    Calls section, further down from the sample payload). In that same sample
    payload, site.instant_power is -23 while the battery (instant_power=1200,
    positive per _battery_export_power's convention) is discharging and, with
    solar's 10W added in, comfortably covers load's 1182.5W with a small
    surplus -- i.e. the home is a net exporter at that instant. So negative
    site/grid instant_power means exporting to the grid, positive means
    importing from it: the reverse of battery_power's charge/discharge sign.
    Don't copy battery's polarity here.
    """
    if data.grid_power is None:
        return None
    return max(data.grid_power, 0)


def _grid_export_power(data: PowerwallData) -> float | None:
    """Power pushed to the grid as a positive value, else 0. See _grid_import_power."""
    if data.grid_power is None:
        return None
    return max(-data.grid_power, 0)


def _battery_health(data: PowerwallData, design_capacity_kwh: float) -> float | None:
    """Current full-charge capacity as a percentage of the installed design capacity."""
    if data.battery_full_pack_energy is None or design_capacity_kwh <= 0:
        return None
    total_design_kwh = design_capacity_kwh * (data.battery_count or 1)
    return round(data.battery_full_pack_energy / total_design_kwh * 100, 1)


# The Tesla app's 0-100% covers the top 95% of the pack (its 0% is 5% raw);
# battery_level_app and battery_reserve are already on that scale.
APP_USABLE_FRACTION = 0.95
MIN_FLOW_W = 50
# Reported when the battery isn't flowing in the sensor's direction; 0 means done.
ETA_NOT_APPLICABLE = -1


@dataclass(frozen=True)
class BatteryEta:
    minutes: int
    energy_kwh: float
    target_level: float


def _battery_eta(
    data: PowerwallData, design_capacity_kwh: float, charging: bool
) -> BatteryEta | None:
    """Time until the battery reaches 100% (charging) or its floor (discharging).

    The floor is the backup reserve while on grid (the gateway stops discharging
    there) and the app's 0% when off grid.
    """
    level = data.battery_level_app
    power = data.battery_power
    if level is None or power is None:
        return None
    flow_w = -power if charging else power
    if flow_w < MIN_FLOW_W:
        return None
    capacity_kwh = data.battery_full_pack_energy or design_capacity_kwh * (data.battery_count or 1)
    if charging:
        target = 100.0
        span = target - level
    else:
        target = (data.battery_reserve or 0.0) if data.grid_connected is not False else 0.0
        span = level - target
    energy_kwh = max(span, 0.0) / 100 * capacity_kwh * APP_USABLE_FRACTION
    return BatteryEta(
        minutes=round(energy_kwh * 1000 / flow_w * 60),
        energy_kwh=round(energy_kwh, 2),
        target_level=target,
    )


def _trapezoidal_energy_delta_kwh(power1: float, power2: float, elapsed_seconds: float) -> float:
    """Trapezoidal-rule energy (kWh) delta between two power (W) samples.

    Averages the two power readings and multiplies by elapsed time, i.e. the
    area of the trapezoid between them -- a closer approximation of the true
    integral than assuming power was constant at either endpoint for the
    whole interval. W * h / 1000 = kWh.
    """
    avg_power_w = (power1 + power2) / 2
    elapsed_hours = elapsed_seconds / 3600
    return avg_power_w * elapsed_hours / 1000


SENSOR_DESCRIPTIONS: tuple[PowerwallSensorDescription, ...] = (
    PowerwallSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.battery_level,
    ),
    PowerwallSensorDescription(
        key="battery_level_app",
        translation_key="battery_level_app",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.battery_level_app,
    ),
    PowerwallSensorDescription(
        key="backup_time_remaining",
        translation_key="backup_time_remaining",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.backup_time_remaining,
    ),
    PowerwallSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.grid_power,
    ),
    PowerwallSensorDescription(
        key="grid_import_power",
        translation_key="grid_import_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grid_import_power,
    ),
    PowerwallSensorDescription(
        key="grid_export_power",
        translation_key="grid_export_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grid_export_power,
    ),
    PowerwallSensorDescription(
        key="solar_power",
        translation_key="solar_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.solar_power,
    ),
    PowerwallSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.battery_power,
    ),
    PowerwallSensorDescription(
        key="battery_import_power",
        translation_key="battery_import_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_battery_import_power,
    ),
    PowerwallSensorDescription(
        key="battery_export_power",
        translation_key="battery_export_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_battery_export_power,
    ),
    PowerwallSensorDescription(
        key="battery_energy_imported",
        translation_key="battery_energy_imported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.battery_energy_imported,
    ),
    PowerwallSensorDescription(
        key="battery_energy_exported",
        translation_key="battery_energy_exported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.battery_energy_exported,
    ),
    PowerwallSensorDescription(
        key="grid_energy_imported",
        translation_key="grid_energy_imported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.grid_energy_imported,
    ),
    PowerwallSensorDescription(
        key="grid_energy_exported",
        translation_key="grid_energy_exported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.grid_energy_exported,
    ),
    PowerwallSensorDescription(
        key="solar_energy_exported",
        translation_key="solar_energy_exported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.solar_energy_exported,
    ),
    PowerwallSensorDescription(
        key="home_energy_imported",
        translation_key="home_energy_imported",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.home_energy_imported,
    ),
    PowerwallSensorDescription(
        key="home_power",
        translation_key="home_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.home_power,
    ),
    PowerwallSensorDescription(
        key="battery_full_pack_energy",
        translation_key="battery_full_pack_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.battery_full_pack_energy,
        attributes_fn=lambda data: {"battery_count": data.battery_count},
    ),
    PowerwallSensorDescription(
        key="grid_status",
        translation_key="grid_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.grid_status,
    ),
    PowerwallSensorDescription(
        key="alert_count",
        translation_key="alert_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: len(data.alerts),
        attributes_fn=lambda data: {"alerts": data.alerts},
    ),
    PowerwallSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.version,
    ),
    PowerwallSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_uptime_seconds,
    ),
)

# Client-side Riemann-sum (trapezoidal) energy estimates, one per power source_fn
# above -- see PowerwallEnergyIntegrationSensor for why these ship disabled by
# default. (key, translation_key, source_fn) rather than a dataclass since the
# sensor class needs the raw source_fn as a constructor arg, not something read
# off a shared PowerwallSensorDescription-style entity_description.
ENERGY_INTEGRATION_SENSORS: tuple[tuple[str, str, Callable[[PowerwallData], float | None]], ...] = (
    ("battery_import_energy", "battery_import_energy", _battery_import_power),
    ("battery_export_energy", "battery_export_energy", _battery_export_power),
    ("grid_import_energy", "grid_import_energy", _grid_import_power),
    ("grid_export_energy", "grid_export_energy", _grid_export_power),
    ("home_energy", "home_energy", lambda data: data.home_power),
)


TARIFF_SENSOR_DESCRIPTIONS: tuple[PowerwallSensorDescription, ...] = tuple(
    PowerwallSensorDescription(
        key=key,
        translation_key=key,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, key=key: getattr(data, key),
    )
    for key in ("tariff_name", "tariff_utility", "tariff_code", "tariff_currency")
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PypowerwallConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up pypowerwall sensors from a config entry."""
    coordinator = entry.runtime_data

    async_add_entities(
        PowerwallSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    )
    design_capacity_kwh = float(
        entry.options.get(CONF_DESIGN_CAPACITY, DEFAULT_DESIGN_CAPACITY_KWH)
    )
    async_add_entities(
        [
            PowerwallBatteryHealthSensor(coordinator, design_capacity_kwh),
            PowerwallBatteryEtaSensor(coordinator, design_capacity_kwh, charging=True),
            PowerwallBatteryEtaSensor(coordinator, design_capacity_kwh, charging=False),
        ]
    )

    if entry.data[CONF_CONN_TYPE] in TARIFF_CONN_TYPES:
        async_add_entities(
            PowerwallSensor(coordinator, description) for description in TARIFF_SENSOR_DESCRIPTIONS
        )

    async_add_entities(
        PowerwallEnergyIntegrationSensor(coordinator, source_fn, key, translation_key)
        for key, translation_key, source_fn in ENERGY_INTEGRATION_SENSORS
    )

    known_temp_devices: set[str] = set()

    @callback
    def _add_new_temp_sensors() -> None:
        new_devices = set(coordinator.data.temps) - known_temp_devices
        if not new_devices:
            return
        known_temp_devices.update(new_devices)
        async_add_entities(PowerwallTempSensor(coordinator, device) for device in new_devices)

    _add_new_temp_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_temp_sensors))

    known_fan_sensors: set[tuple[str, str]] = set()

    @callback
    def _add_new_fan_sensors() -> None:
        new_fans = {
            (device, signal)
            for device, signals in coordinator.data.fans.items()
            for signal in signals
        } - known_fan_sensors
        if not new_fans:
            return
        known_fan_sensors.update(new_fans)
        async_add_entities(
            PowerwallFanSensor(coordinator, device, signal) for device, signal in new_fans
        )

    _add_new_fan_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_fan_sensors))


class PowerwallSensor(PowerwallEntity, SensorEntity):
    """A pypowerwall sensor driven by a PowerwallSensorDescription."""

    entity_description: PowerwallSensorDescription

    def __init__(
        self, coordinator: PowerwallDataUpdateCoordinator, description: PowerwallSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._din}_{description.key}"

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)


class PowerwallBatteryHealthSensor(PowerwallEntity, SensorEntity):
    """Full-charge capacity as a percentage of design capacity (state of health)."""

    _attr_translation_key = "battery_health"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-heart-variant"

    def __init__(
        self, coordinator: PowerwallDataUpdateCoordinator, design_capacity_kwh: float
    ) -> None:
        super().__init__(coordinator)
        self._design_capacity_kwh = design_capacity_kwh
        self._attr_unique_id = f"{self._din}_battery_health"

    @property
    def native_value(self) -> float | None:
        return _battery_health(self.coordinator.data, self._design_capacity_kwh)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data
        count = data.battery_count or 1
        return {
            "full_pack_energy_kwh": data.battery_full_pack_energy,
            "design_capacity_kwh": round(self._design_capacity_kwh * count, 3),
            "design_capacity_per_battery_kwh": self._design_capacity_kwh,
            "battery_count": data.battery_count,
        }


class PowerwallBatteryEtaSensor(PowerwallEntity, SensorEntity):
    """Minutes until the battery is full, or down to its reserve, at the current rate."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: PowerwallDataUpdateCoordinator,
        design_capacity_kwh: float,
        charging: bool,
    ) -> None:
        super().__init__(coordinator)
        self._design_capacity_kwh = design_capacity_kwh
        self._charging = charging
        key = "battery_time_to_full" if charging else "battery_time_to_discharged"
        self._attr_translation_key = key
        self._attr_unique_id = f"{self._din}_{key}"
        self._attr_icon = "mdi:battery-clock" if charging else "mdi:battery-clock-outline"

    def _eta(self) -> BatteryEta | None:
        return _battery_eta(self.coordinator.data, self._design_capacity_kwh, self._charging)

    @property
    def native_value(self) -> int | None:
        eta = self._eta()
        if eta is not None:
            return eta.minutes
        data = self.coordinator.data
        if data.battery_level_app is None or data.battery_power is None:
            return None
        return ETA_NOT_APPLICABLE

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        eta = self._eta()
        if eta is None:
            return {"finish_time": None, "energy_kwh": None, "target_level": None}
        finish = dt_util.now() + timedelta(minutes=eta.minutes)
        return {
            "finish_time": finish.replace(second=0, microsecond=0).isoformat(),
            "energy_kwh": eta.energy_kwh,
            "target_level": eta.target_level,
        }


class PowerwallTempSensor(PowerwallEntity, SensorEntity):
    """A temperature sensor for a single Powerwall battery device."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PowerwallDataUpdateCoordinator, device: str) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{self._din}_temp_{device}"
        self._attr_translation_key = "battery_temp"
        self._attr_translation_placeholders = {"device": device}

    @property
    def native_value(self):
        return self.coordinator.data.temps.get(self._device)

    @property
    def available(self) -> bool:
        return super().available and self._device in self.coordinator.data.temps


class PowerwallFanSensor(PowerwallEntity, SensorEntity):
    """A fan speed (RPM) or drive duty cycle (%) sensor for one Powerwall 3 inverter fan."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: PowerwallDataUpdateCoordinator, device: str, signal: str
    ) -> None:
        super().__init__(coordinator)
        if signal not in FAN_SIGNALS:
            raise ValueError(f"Unknown fan signal: {signal}")
        kind, fan = signal.removeprefix("PCH_Fan").split("_")
        self._device = device
        self._signal = signal
        self._attr_unique_id = f"{self._din}_{signal}_{device}"
        self._attr_translation_key = "fan_speed" if kind == "Speed" else "fan_duty"
        self._attr_translation_placeholders = {"device": device, "fan": fan}
        self._attr_native_unit_of_measurement = (
            REVOLUTIONS_PER_MINUTE if kind == "Speed" else PERCENTAGE
        )

    @property
    def native_value(self):
        return self.coordinator.data.fans.get(self._device, {}).get(self._signal)

    @property
    def available(self) -> bool:
        return super().available and self._device in self.coordinator.data.fans


class PowerwallEnergyIntegrationSensor(PowerwallEntity, RestoreEntity, SensorEntity):
    """Client-side Riemann-sum (trapezoidal) energy estimate for a power source_fn.

    Unlike every other sensor in this platform, this one is stateful: it keeps a
    running total plus the last (power, timestamp) sample so it can integrate
    the trapezoid between consecutive coordinator updates, and it must survive
    Home Assistant restarts without resetting to zero since it's an accumulator
    -- hence RestoreEntity, restoring the running total from the last known
    state in async_added_to_hass rather than the usual pure coordinator-data
    read every other sensor here uses.

    This exists as a fallback for battery_energy_imported/_exported (and a
    same-idea addition for grid/home), which read the gateway's own lifetime
    meter counters and are more accurate -- but on some hardware those fields
    don't populate reliably. A client-side power integration is a strictly
    worse approximation (missed samples between polls, no sub-interval
    resolution) than the real meter, so this ships opt-in only:
    _attr_entity_registry_enabled_default = False on every instance. Don't
    flip that default without discussion -- it's the deliberate point of this
    class.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PowerwallDataUpdateCoordinator,
        source_fn: Callable[[PowerwallData], float | None],
        key: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._source_fn = source_fn
        self._attr_unique_id = f"{self._din}_{key}"
        self._attr_translation_key = translation_key
        self._total_kwh = 0.0
        self._last_power: float | None = None
        self._last_updated: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            try:
                self._total_kwh = float(last_state.state)
            except ValueError:
                self._total_kwh = 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        current_power = self._source_fn(self.coordinator.data)
        now = dt_util.utcnow()
        if self._last_power is not None and current_power is not None:
            elapsed_seconds = (now - self._last_updated).total_seconds()
            self._total_kwh += _trapezoidal_energy_delta_kwh(
                self._last_power, current_power, elapsed_seconds
            )
        if current_power is not None:
            self._last_power = current_power
            self._last_updated = now
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return round(self._total_kwh, 3)
