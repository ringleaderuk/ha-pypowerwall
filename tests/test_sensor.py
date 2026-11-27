from custom_components.pypowerwall.coordinator import PowerwallData
from custom_components.pypowerwall.sensor import (
    _battery_eta,
    _battery_export_power,
    _battery_import_power,
    _grid_export_power,
    _grid_import_power,
    _trapezoidal_energy_delta_kwh,
    _uptime_seconds,
)


class TestBatteryImportExportPower:
    def test_charging_is_import_only(self):
        data = PowerwallData(battery_power=-500)
        assert _battery_import_power(data) == 500
        assert _battery_export_power(data) == 0

    def test_discharging_is_export_only(self):
        data = PowerwallData(battery_power=750)
        assert _battery_import_power(data) == 0
        assert _battery_export_power(data) == 750

    def test_idle_battery_is_zero_both_ways(self):
        data = PowerwallData(battery_power=0)
        assert _battery_import_power(data) == 0
        assert _battery_export_power(data) == 0

    def test_missing_battery_power_is_none_both_ways(self):
        data = PowerwallData(battery_power=None)
        assert _battery_import_power(data) is None
        assert _battery_export_power(data) is None


class TestGridImportExportPower:
    """Grid's sign convention is the OPPOSITE of battery's: negative grid_power
    means exporting to the grid, positive means importing -- see sensor.py's
    _grid_import_power docstring for the derivation from pypowerwall's own
    README. These cases mirror pypowerwall's example /api/meters/aggregates
    payload (site.instant_power=-23 while net-exporting), and would fail if the
    sign handling were accidentally copy-pasted from battery's convention.
    """

    def test_negative_grid_power_is_export_only(self):
        data = PowerwallData(grid_power=-23)
        assert _grid_import_power(data) == 0
        assert _grid_export_power(data) == 23

    def test_positive_grid_power_is_import_only(self):
        data = PowerwallData(grid_power=750)
        assert _grid_import_power(data) == 750
        assert _grid_export_power(data) == 0

    def test_idle_grid_is_zero_both_ways(self):
        data = PowerwallData(grid_power=0)
        assert _grid_import_power(data) == 0
        assert _grid_export_power(data) == 0

    def test_missing_grid_power_is_none_both_ways(self):
        data = PowerwallData(grid_power=None)
        assert _grid_import_power(data) is None
        assert _grid_export_power(data) is None


class TestTrapezoidalEnergyDeltaKwh:
    def test_constant_power_for_one_hour(self):
        # 1000W held constant for 3600s (1h) = 1 kWh.
        assert _trapezoidal_energy_delta_kwh(1000, 1000, 3600) == 1.0

    def test_ramping_power_averages_the_two_samples(self):
        # (0W + 2000W) / 2 = 1000W average, held for 1800s (0.5h) = 0.5 kWh.
        assert _trapezoidal_energy_delta_kwh(0, 2000, 1800) == 0.5

    def test_zero_elapsed_time_is_zero_delta(self):
        assert _trapezoidal_energy_delta_kwh(500, 1500, 0) == 0.0

    def test_short_interval_matches_hand_computed_value(self):
        # (100W + 300W) / 2 = 200W average, held for 10s -> 200 * (10/3600) / 1000 kWh.
        result = _trapezoidal_energy_delta_kwh(100, 300, 10)
        assert result == 200 * (10 / 3600) / 1000


class TestFanSensor:
    def _sensor(self, signal, fans):
        from unittest.mock import MagicMock

        from custom_components.pypowerwall.sensor import PowerwallFanSensor

        coordinator = MagicMock()
        coordinator.data = PowerwallData(din="DIN1", fans=fans)
        return PowerwallFanSensor(coordinator, "TEPINV--x", signal)

    def test_speed_is_rpm(self):
        s = self._sensor("PCH_FanSpeed_B", {"TEPINV--x": {"PCH_FanSpeed_B": 1397}})
        assert s.native_unit_of_measurement == "rpm"
        assert s.native_value == 1397
        assert s.translation_key == "fan_speed"
        assert s.translation_placeholders == {"device": "TEPINV--x", "fan": "B"}

    def test_duty_is_percent(self):
        s = self._sensor("PCH_FanDuty_A", {"TEPINV--x": {"PCH_FanDuty_A": 19.1}})
        assert s.native_unit_of_measurement == "%"
        assert s.translation_key == "fan_duty"
        assert s.native_value == 19.1

    def test_missing_device_is_none(self):
        s = self._sensor("PCH_FanDuty_A", {})
        assert s.native_value is None


class TestBatteryHealth:
    def _sensor(self, full_pack, count, design=13.5):
        from unittest.mock import MagicMock

        from custom_components.pypowerwall.sensor import PowerwallBatteryHealthSensor

        coordinator = MagicMock()
        coordinator.data = PowerwallData(
            din="DIN1", battery_full_pack_energy=full_pack, battery_count=count
        )
        return PowerwallBatteryHealthSensor(coordinator, design)

    def test_single_battery(self):
        s = self._sensor(12.15, 1)
        assert s.native_value == 90.0
        assert s.native_unit_of_measurement == "%"
        assert s.extra_state_attributes["design_capacity_kwh"] == 13.5

    def test_scales_design_capacity_by_battery_count(self):
        s = self._sensor(25.65, 2)
        assert s.native_value == 95.0
        assert s.extra_state_attributes["design_capacity_kwh"] == 27.0

    def test_unknown_count_assumes_one(self):
        assert self._sensor(13.5, None).native_value == 100.0

    def test_custom_design_capacity(self):
        assert self._sensor(13.0, 1, design=14.0).native_value == 92.9

    def test_missing_capacity_is_none(self):
        assert self._sensor(None, 1).native_value is None


def _eta_data(**overrides):
    values = {
        "battery_level_app": 50.0,
        "battery_reserve": 20.0,
        "battery_power": 0,
        "battery_full_pack_energy": 12.0,
        "battery_count": 1,
        "grid_connected": True,
    }
    values.update(overrides)
    return PowerwallData(**values)


class TestBatteryEta:
    def test_charging_to_full(self):
        eta = _battery_eta(_eta_data(battery_power=-2850), 13.5, charging=True)
        assert eta.target_level == 100.0
        assert eta.energy_kwh == 5.7
        assert eta.minutes == 120

    def test_discharging_stops_at_reserve_on_grid(self):
        eta = _battery_eta(_eta_data(battery_power=1140), 13.5, charging=False)
        assert eta.target_level == 20.0
        assert eta.energy_kwh == 3.42
        assert eta.minutes == 180

    def test_discharging_to_app_zero_off_grid(self):
        eta = _battery_eta(
            _eta_data(battery_power=1140, grid_connected=False), 13.5, charging=False
        )
        assert eta.target_level == 0.0
        assert eta.minutes == 300

    def test_wrong_direction_or_idle_is_none(self):
        assert _battery_eta(_eta_data(battery_power=1000), 13.5, charging=True) is None
        assert _battery_eta(_eta_data(battery_power=-1000), 13.5, charging=False) is None
        assert _battery_eta(_eta_data(battery_power=20), 13.5, charging=False) is None

    def test_below_reserve_is_zero(self):
        eta = _battery_eta(
            _eta_data(battery_power=500, battery_level_app=15.0), 13.5, charging=False
        )
        assert eta.minutes == 0

    def test_falls_back_to_design_capacity(self):
        eta = _battery_eta(
            _eta_data(battery_power=-2565, battery_full_pack_energy=None), 13.5, charging=True
        )
        assert eta.energy_kwh == 6.41
        assert eta.minutes == 150

    def test_missing_level_is_none(self):
        data = _eta_data(battery_power=-1000, battery_level_app=None)
        assert _battery_eta(data, 13.5, charging=True) is None

    def test_sensor_value_and_attributes(self):
        from unittest.mock import MagicMock

        from custom_components.pypowerwall.sensor import PowerwallBatteryEtaSensor

        coordinator = MagicMock()
        coordinator.data = _eta_data(din="DIN1", battery_power=1140)
        sensor = PowerwallBatteryEtaSensor(coordinator, 13.5, charging=False)
        assert sensor.native_value == 3.0
        assert sensor.native_unit_of_measurement == "h"
        assert sensor.extra_state_attributes["minutes"] == 180
        attrs = sensor.extra_state_attributes
        assert attrs["target_level"] == 20.0
        assert attrs["finish_time"] is not None

        coordinator.data = _eta_data(din="DIN1", battery_power=-1000)
        assert sensor.native_value == -1
        assert sensor.extra_state_attributes["finish_time"] is None

        coordinator.data = _eta_data(din="DIN1", battery_power=10)
        assert sensor.native_value == -1

        coordinator.data = _eta_data(din="DIN1", battery_power=None)
        assert sensor.native_value is None


class TestAlertAttributes:
    def _sensor(self, key, alerts):
        from unittest.mock import MagicMock

        from custom_components.pypowerwall.sensor import SENSOR_DESCRIPTIONS, PowerwallSensor

        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        coordinator = MagicMock()
        coordinator.data = PowerwallData(din="DIN1", alerts=alerts)
        return PowerwallSensor(coordinator, description)

    def test_alert_names_exposed_as_attribute(self):
        s = self._sensor("alert_count", ["GridCodesWrite", "PodCommissionTime"])
        assert s.native_value == 2
        assert s.extra_state_attributes == {"alerts": ["GridCodesWrite", "PodCommissionTime"]}

    def test_no_alerts_gives_empty_list(self):
        s = self._sensor("alert_count", [])
        assert s.native_value == 0
        assert s.extra_state_attributes == {"alerts": []}

    def test_other_sensors_have_no_attributes(self):
        assert self._sensor("firmware_version", []).extra_state_attributes is None


class TestAlertBinarySensors:
    CASES = [
        ("battery_calibration", "BatteryCalibration", None),
        ("battery_fault", "BatteryFault", "problem"),
        ("grid_manually_disconnected", "ScheduledIslandContactorOpen", "none"),
        ("self_consumption_reserve_limit", "SelfConsumptionReservedLimit", None),
        ("solar_charge_only_limited", "SolarChargeOnlyLimited", None),
        ("backfeed_limited", "BackfeedLimited", None),
        ("site_min_power_limited", "SiteMinPowerLimited", None),
    ]

    def _sensor(self, key, alerts):
        from unittest.mock import MagicMock

        from custom_components.pypowerwall.binary_sensor import (
            ALERT_BINARY_SENSOR_DESCRIPTIONS,
            PowerwallAlertBinarySensor,
        )

        description = next(d for d in ALERT_BINARY_SENSOR_DESCRIPTIONS if d.key == key)
        coordinator = MagicMock()
        coordinator.data = PowerwallData(din="DIN1", alerts=alerts)
        return PowerwallAlertBinarySensor(coordinator, description)

    def test_on_only_while_its_alert_is_active(self):
        for key, alert, _ in self.CASES:
            assert self._sensor(key, [alert, "GridCodesWrite"]).is_on is True
            assert self._sensor(key, ["GridCodesWrite"]).is_on is False
            assert self._sensor(key, []).is_on is False

    def test_each_sensor_ignores_the_others_alerts(self):
        others = [alert for _, alert, _ in self.CASES]
        assert (
            self._sensor("battery_fault", [a for a in others if a != "BatteryFault"]).is_on is False
        )

    def test_diagnostic_and_disabled_by_default(self):
        from homeassistant.const import EntityCategory

        for key, _, _ in self.CASES:
            sensor = self._sensor(key, [])
            assert sensor.entity_registry_enabled_default is False
            assert sensor.entity_category is EntityCategory.DIAGNOSTIC

    def test_battery_calibration_also_follows_calibration_requested(self):
        both = ["BatteryCalibration", "CalibrationRequested"]
        assert self._sensor("battery_calibration", ["CalibrationRequested"]).is_on is True
        assert self._sensor("battery_calibration", both).is_on is True

    def test_battery_fault_is_a_problem_sensor(self):
        assert self._sensor("battery_fault", []).device_class == "problem"

    def test_unique_id_keeps_the_original_battery_calibration_suffix(self):
        assert self._sensor("battery_calibration", []).unique_id.endswith("_battery_calibration")


class TestUptimeSeconds:
    def test_go_duration_string(self):
        assert _uptime_seconds(PowerwallData(uptime="62h48m24.076725628s")) == 226104

    def test_minutes_and_seconds_only(self):
        assert _uptime_seconds(PowerwallData(uptime="5m30s")) == 330

    def test_plain_number_of_seconds(self):
        assert _uptime_seconds(PowerwallData(uptime="1234")) == 1234
        assert _uptime_seconds(PowerwallData(uptime=1234)) == 1234

    def test_missing_or_unparseable_is_none(self):
        assert _uptime_seconds(PowerwallData(uptime=None)) is None
        assert _uptime_seconds(PowerwallData(uptime="1 day, 2:03:04")) is None
        assert _uptime_seconds(PowerwallData(uptime="")) is None
