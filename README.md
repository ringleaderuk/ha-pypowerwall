# ha-pypowerwall

<img src="custom_components/pypowerwall/brand/icon.png" alt="pypowerwall icon" width="120" align="right" />

[![GitHub release](https://img.shields.io/github/v/release/jackrayner/ha-pypowerwall)](https://github.com/jackrayner/ha-pypowerwall/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz/)
[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jackrayner&repository=ha-pypowerwall&category=integration)

A [HACS](https://hacs.xyz/) custom integration for Home Assistant that connects to a Tesla Powerwall gateway using [pypowerwall](https://github.com/jasonacox/pypowerwall). Home Assistant polls the gateway (or Tesla's cloud) directly and exposes battery, grid, solar, and home power as native entities. Supports every connection mode pypowerwall offers: local TEDAPI, customer login, hybrid, cloud, FleetAPI, and TEDAPI v1r LAN.

## Installation

### Via HACS

1. Click the "Open in HACS" badge above, or in HACS search for "Tesla Powerwall (pypowerwall)".
2. Download it and restart Home Assistant.

### Manual

Copy `custom_components/pypowerwall/` into your Home Assistant `config/custom_components/` directory and restart.

## Setup

Settings → Devices & Services → Add Integration → "Tesla Powerwall (pypowerwall)" → pick a connection type.

Poll interval defaults to 5s (60s for Cloud/FleetAPI, to avoid hammering Tesla's rate-limited cloud API) and is configurable afterward via the integration's Options (2–300 seconds).

### Connection types

| Type | What it needs | Setup |
| --- | --- | --- |
| **TEDAPI** (recommended) | Gateway host + the Wi-Fi password from the gateway's QR sticker | Nothing else — works out of the box on Powerwall 2, Powerwall+, and Powerwall 3. |
| **Hybrid** | The above, plus a Customer Login email/password | Adds supplemental vitals on top of TEDAPI. Requires Customer Login enabled on the gateway. |
| **Local login** | Gateway host + Customer Login email/password | Requires [Customer Login](https://www.tesla.com/support/energy/powerwall/mobile-app/monitoring-from-home-network) enabled on the gateway first (Tesla app or gateway web UI). |
| **Cloud mode (sign in)** | Your Tesla account | Sign in from Home Assistant: the form links to Tesla's login page, and you paste back the callback line your browser's developer console shows. The integration saves the tokens to `/config/pypowerwall/.pypowerwall.auth`. See [Cloud sign-in](#cloud-sign-in). |
| **Cloud mode (existing auth file)** | A directory containing a `.pypowerwall.auth` file | For a file you already have: run `python -m pypowerwall setup` (or `setup -headless` if you can't open a browser on the machine) to log into your Tesla account and create it, then point the integration at its directory. |
| **FleetAPI** | A directory containing a `.pypowerwall.fleetapi` file | One-time setup outside Home Assistant: register an app at [developer.tesla.com](https://developer.tesla.com), then run `python -m pypowerwall.fleetapi setup` to complete the OAuth flow and create that file. |
| **TEDAPI v1r LAN (set up with Tesla account)** | Your Tesla account, physical access to the Powerwall, then the gateway's LAN IP + gateway password | Home Assistant creates and registers the RSA key for you. See [v1r setup in Home Assistant](#v1r-setup-in-home-assistant). |
| **TEDAPI v1r LAN (existing RSA key)** | Gateway host + gateway password and/or customer password (at least one) + an RSA private key path | One-time setup outside Home Assistant: run `python -m pypowerwall register` to generate and register an RSA-4096 key pair with the gateway (may require briefly power-cycling it to confirm), then point the integration at the resulting `.pem` (see pypowerwall's [v1r setup guide](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/README.md#v1r-lan-tedapi-setup---option-5-powerwall-3-wired-lan) and [key states](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/README.md#key-states)). The optional WiFi fallback host is described under [v1r WiFi Fallback](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/README.md#v1r-wifi-fallback). Powerwall 3, and Powerwall 2 with a Gateway 2 (reported working by pypowerwall users on firmware 26.26.4; other gateways untested). If only the gateway password is given, its last 5 characters are used as the customer login password. |

Note: Cloud and FleetAPI modes poll Tesla's cloud API rather than the local gateway, even though the integration as a whole is classified `local_polling` in its manifest (Home Assistant only allows one `iot_class` per integration, and 4 of the 6 connection types above are genuinely local).

FleetAPI and TEDAPI v1r authenticate via an artifact pypowerwall's own CLI setup tools produce. Run the relevant `setup`/`register` command once, on any machine, then tell the integration where the resulting file lives. Cloud mode can do the same, or sign in from Home Assistant directly.

### Cloud sign-in

Pick **Cloud mode (sign in with Tesla account)** in the connection menu, then, in Chrome or Edge on a computer:

1. Open the sign-in link on the form.
2. On Tesla's tab, press **F12** and open the **Console** tab.
3. Log in to your Tesla account. At the end the page doesn't change: Tesla redirects to `tesla://auth/callback?code=...`, which browsers can't open.
4. The Console shows a red line, `Failed to launch 'tesla://auth/callback?code=...' because the scheme does not have a registered handler.` Copy the whole line and paste it into the form.

Home Assistant exchanges the code for tokens, writes `/config/pypowerwall/.pypowerwall.auth`, and connects. Paste within a few minutes. Each link works once, and a failed attempt shows a fresh one.

Tesla only accepts the `tesla://` redirect for this login. The older `https://auth.tesla.com/void/callback` "Page Not Found" redirect is rejected with "The 'redirect_uri' supplied is not registered for this 'client_id'".

To sign in again later (for example after the login expired), open the entry's **Reconfigure** and choose **Sign in to Tesla again**. This rewrites the entry's own auth file and reloads it.

### v1r setup in Home Assistant

Pick **TEDAPI v1r LAN (set up with Tesla account)** in the connection menu. It replaces `python -m pypowerwall setup -v1r`:

1. Sign in to Tesla the same way as [Cloud sign-in](#cloud-sign-in).
2. Home Assistant creates an RSA-4096 key at `/config/pypowerwall/tedapi_rsa_private.pem` (or reuses the one already there) and asks Tesla to register it with your gateway. If the account has more than one energy site, you choose which.
3. Within about 10 minutes, turn the Powerwall's On/Off switch off for about 15 seconds and back on, then select **Submit**. On Powerwall 2 it's the switch on the side of the battery; on Powerwall 3 it's the small black rocker under the red flap, not the large red knob. Home Assistant watches for the key to be approved for up to 2 minutes.
4. Enter the gateway's LAN IP address and the gateway password from its QR label.

If the 10 minutes run out, the same key is sent again and you can toggle the switch again. If Tesla approves the key straight away, step 3 is skipped. The Tesla sign-in is only used for registration and isn't saved.

For Cloud and FleetAPI, enter the directory **as Home Assistant sees it** (for example `/config/pypowerwall` on Home Assistant OS or a container, not the path on the machine where you ran `setup`). The directory must be writable, because pypowerwall saves refreshed Tesla tokens back into it, and persistent: on Home Assistant OS or a container, keep it under `/config` (or `/share`), since other paths are inside the container and are wiped when Home Assistant updates. Tesla invalidates each refresh token once it's used, so give Home Assistant its own copy of the file rather than sharing one with pypowerwall running elsewhere (each copy's refreshes break the other). Cloud mode is selected by picking "Cloud mode" in the connection menu; no gateway host is needed.

### Cloud mode troubleshooting

- **"No .pypowerwall.auth … file found"**: the path isn't visible to Home Assistant, or the file is missing. Copy `.pypowerwall.auth` into a directory under `/config` and enter that path.
- **"Unable to connect to Powerwall (cloud mode)" with a valid file**: the login in the file has probably expired (a log line such as `Login failure - MissingCodeError` means Tesla rejected it). Use **Reconfigure → Sign in to Tesla again**, or re-run `python -m pypowerwall setup` and replace the file. A running Cloud entry picks up the new file within about five minutes of its next failed poll, without a restart.

### Localization

The integration ships a translation file for every language Home Assistant supports and follows your Home Assistant instance's configured language automatically (Settings → System → General → Language) — no per-integration language setting. Coverage of less common languages is machine-translated rather than native-checked; if you spot an awkward or incorrect string in your language, please open an issue or PR.

## Entities

| Entity | Platform | Notes |
| --- | --- | --- |
| Battery level | sensor | `%`, from customer API scale |
| Battery level (Tesla app) | sensor | `%`, app-scaled value |
| Battery full pack energy | sensor | `kWh`, the battery's current full-charge capacity (`nominal_full_pack_energy`, summed over all Powerwalls). Attribute `battery_count`. |
| Battery health | sensor | `%`, full pack energy ÷ (design capacity × number of Powerwalls). Design capacity per Powerwall is set in the integration's Options (default 13.5 kWh, the rated capacity of Powerwall 2 and 3). This is the battery's own estimate, so it drifts a little with temperature; the long-term trend is the useful part. |
| Battery time to full | sensor | `h` (like Backup time remaining), at the current charge rate, to 100% on the Tesla app scale. `-1` when not charging (discharging, or idle under 50 W); `0` means full. Attributes `minutes`, `finish_time`, `energy_kwh`, `target_level`. |
| Battery time to discharged | sensor | `h`, at the current discharge rate, down to the backup reserve while on grid, or to the app's 0% (5% raw) when off grid. `-1` when not discharging; `0` means at the floor. Same attributes. Uses full pack energy, or the Options design capacity when that isn't reported. |
| Battery reserve | number | `%`, 0-100, writable — sets the backup reserve |
| Battery mode | select | `self_consumption` / `backup` / `autonomous`, writable — sets the operation mode |
| Grid charging | switch | writable — allow/disallow charging the battery from the grid. **Cloud, FleetAPI and TEDAPI v1r LAN modes only** (in v1r it is a config write to the gateway over the LAN, no cloud account needed). |
| Grid export | select | `battery_ok` / `pv_only` / `never`, writable — sets the grid export policy. **Cloud, FleetAPI and TEDAPI v1r LAN modes only.** |
| Backup time remaining | sensor | hours |
| Grid power | sensor | W, signed — negative while exporting, positive while importing |
| Grid import power | sensor | W, the importing portion of grid power as a positive value (0 when exporting or idle) |
| Grid export power | sensor | W, the exporting portion of grid power as a positive value (0 when importing or idle) |
| Solar power | sensor | W |
| Battery power | sensor | W, signed — negative while charging, positive while discharging |
| Battery import power | sensor | W, the charging portion of battery power as a positive value (0 when discharging or idle) |
| Battery export power | sensor | W, the discharging portion of battery power as a positive value (0 when charging or idle) |
| Battery energy charged | sensor | kWh, cumulative, `total_increasing` — the gateway's own lifetime battery-meter counter, not a client-side estimate. Use for the Energy dashboard's "energy going into the battery." |
| Battery energy discharged | sensor | kWh, cumulative, `total_increasing`, same source. Use for the Energy dashboard's "energy coming out of the battery." |
| Grid energy imported | sensor | kWh, cumulative, `total_increasing` — the gateway's own lifetime site-meter counter. Use for the Energy dashboard's "grid consumption." |
| Grid energy exported | sensor | kWh, cumulative, `total_increasing`, same source. Use for the Energy dashboard's "return to grid." |
| Solar energy produced | sensor | kWh, cumulative, `total_increasing` — the gateway's own lifetime solar-meter counter. Use for the Energy dashboard's "solar production." |
| Home energy consumed | sensor | kWh, cumulative, `total_increasing` — the gateway's own lifetime load counter. |
| Battery import energy (estimated) | sensor | kWh, cumulative, `total_increasing`. **⚠️ Disabled by default.** A client-side trapezoidal (Riemann sum) integration of battery import power, updated once per poll. See callout below. |
| Battery export energy (estimated) | sensor | kWh, cumulative, `total_increasing`. **⚠️ Disabled by default.** Same as above, integrating battery export power. |
| Grid import energy (estimated) | sensor | kWh, cumulative, `total_increasing`. **⚠️ Disabled by default.** Same as above, integrating grid import power. |
| Grid export energy (estimated) | sensor | kWh, cumulative, `total_increasing`. **⚠️ Disabled by default.** Same as above, integrating grid export power. |
| Home energy (estimated) | sensor | kWh, cumulative, `total_increasing`. **⚠️ Disabled by default.** Same as above, integrating home power directly (load is always consumption, no import/export split). |
| Home power | sensor | W |
| Grid status | sensor | `UP` / `DOWN` / `SYNCING` |
| Grid connected | binary_sensor | connectivity, derived from grid status |
| Battery calibration | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports the `CalibrationRequested` or `BatteryCalibration` alert (see [`docs/alerts.md`](./docs/alerts.md)). |
| Battery fault | binary_sensor | **⚠️ Disabled by default.** Diagnostic, `problem` class, on while the gateway reports the `BatteryFault` alert (one or more inverter blocks faulted). |
| Manually disconnected from grid | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports `ScheduledIslandContactorOpen`, which separates a deliberate disconnect from a grid outage. |
| Battery at reserve limit | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports `SelfConsumptionReservedLimit` (the battery reached its reserve limit in self-consumption mode and the home switched to the grid). |
| Solar charge only limited | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports `SolarChargeOnlyLimited` (the system is set to charge only from solar and solar isn't available). |
| Backfeed limited | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports `BackfeedLimited` (the system is configured for inadvertent export and won't discharge further to respect that limit). |
| Site minimum power limited | binary_sensor | **⚠️ Disabled by default.** Diagnostic, on while the gateway reports `SiteMinPowerLimited` (a command couldn't be met because the site minimum power limit is set). |
| Active alerts | sensor | count of active alerts; the alert names (e.g. `GridCodesWrite`) are in the `alerts` attribute, sorted, for templates and automations; see [`docs/alerts.md`](./docs/alerts.md) for what the known alerts mean |
| Firmware version | sensor | diagnostic |
| Uptime | sensor | diagnostic, seconds |
| Tariff name, utility, code, currency | sensor | diagnostic, the site's utility tariff from `get_tariff()` ([fields](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/API.md#tesla-tariff-and-time-of-use-settings)). **Cloud and FleetAPI modes only.** |
| `<device>` temperature | sensor | one per battery pack reported by `vitals()`, added dynamically |
| `<device>` fan A/B speed, duty | sensor | **⚠️ Disabled by default.** Diagnostic RPM and drive duty-cycle (%) for each Powerwall 3 inverter fan, added dynamically. Needs a TEDAPI-backed mode with the default `V2024_06` API version ([why](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/RELEASE.md)); not reported by Powerwall 2, Cloud, or FleetAPI. |
| Reconnect to grid | button | physically closes the grid contactor, reconnecting the home to the utility grid |
| Disconnect from grid | button | **⚠️ Disabled by default.** See callout below before enabling. |

### Estimated energy sensors

The five "(estimated)" energy sensors above are a **fallback** for the gateway-counter energy sensors (battery, grid, home): on some hardware, the gateway's own lifetime meter counters (`energy_imported`/`energy_exported` in `/api/meters/aggregates`) don't populate reliably, so these sensors client-side integrate the corresponding power sensor (trapezoidal/Riemann-sum: average of two consecutive power readings × elapsed time between them) as an approximation instead. They're a strictly less accurate substitute — missed samples between polls and no sub-interval resolution, versus the gateway's own hardware meter — so they ship **disabled by default**; enable the ones you need via Settings → Devices & Services → Entities → find it → enable, only if the real-meter sensors aren't working for you. Their running totals persist across Home Assistant restarts (`RestoreEntity`), same as any other `total_increasing` energy sensor.

### Energy dashboard

To add the battery to Home Assistant's Energy dashboard, go to Settings → Dashboards → Energy → Battery → Add battery, and pick "Battery energy charged" for "Energy going into the battery" and "Battery energy discharged" for "Energy coming out of the battery". For the grid, use "Grid energy imported" and "Grid energy exported"; for solar production, "Solar energy produced". All of these are the gateway's own lifetime counters, so they keep counting while Home Assistant is down, unlike a Riemann-sum integration of the power sensors.

**⚠️ "Disconnect from grid" physically opens the Powerwall's grid contactor.** Pressing it islands the home from the utility grid: solar keeps producing and the battery serves home load, but there's a real-world ~30 second solar production dropout while the contactor switches over, and the home stays off-grid until "Reconnect to grid" is pressed (or the gateway is otherwise commanded to reconnect). Because a button press is irreversible-in-the-moment and affects the physical grid connection, this entity ships **disabled** — it will not appear as an active entity until you explicitly enable it via its entity settings (Settings → Devices & Services → Entities → find it → enable). Note that as of pypowerwall 0.16.1 this method isn't yet implemented by any backend (local/TEDAPI/hybrid/cloud/FleetAPI) and currently no-ops with a logged error regardless of connection mode; the entity is included as forward-compatible surface for whenever a backend adds support, and is intentionally not gated by connection type since none is currently known to work.

Two more write actions are exposed as [Home Assistant actions/services](https://www.home-assistant.io/docs/scripts/service-calls/) rather than entities, since they're momentary rather than persistent state, and only available in **TEDAPI v1r LAN mode** (see pypowerwall's [backup events docs](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/API.md#backup-events-v1r-mode-only)):

| Service | Fields | Notes |
| --- | --- | --- |
| `pypowerwall.schedule_max_backup` | `duration_seconds` (optional, default `7200`) | Starts a manual backup event (storm watch / max backup mode) for the given duration. |
| `pypowerwall.cancel_max_backup` | none | Cancels the current manual backup event. |

### Export window (forced export)

Powerwalls have no "export now" command. In Time-Based Control the Powerwall exports when the tariff Tesla holds for the site makes the current time the most valuable time to sell, so these two actions change that tariff for a while. They are for events the Tesla tariff doesn't know about, such as an Octopus Power Down event or a Saving Session outside peak hours.

| Service | Fields | Notes |
| --- | --- | --- |
| `pypowerwall.set_export_window` | `end` (required), `start` (optional, default now), `price` (optional, default `2.0`) | Saves the site's tariff, then sends Tesla a copy in which `start`–`end` has its own period at `price` per kWh, for both buying and selling. The saved tariff is restored at `end`. |
| `pypowerwall.restore_tariff` | none | Restores the saved tariff now, ending the window early. Does nothing if no window is in place. |

- **Set-up:** Time-Based Control, export set to Everything, and a time-of-use tariff with export rates already in the Tesla app. The window is cut out of the existing periods, so the rest of the day keeps its real prices.
- **Price:** set it well above your normal rates. Buy and sell get the same price because Tesla rejects a sell price above the buy price. The high buy price also stops the Powerwall importing during the window.
- **Timing:** Tesla takes a few minutes to re-plan after a tariff change, so call it at least 10 minutes before the window starts. How much the Powerwall exports is still the optimiser's decision; it exports down to the backup reserve at most.
- **Restore:** the original tariff is kept in Home Assistant storage until it has been restored. If Home Assistant restarts during a window, the tariff is restored when the integration loads again, or at `end` if that's still to come. A failed restore is retried every 5 minutes.
- **The Tesla app** shows the temporary `EXPORT_WINDOW` rates while the window is in place.
- **Connection modes:** tariff changes go through Tesla's cloud. Cloud and FleetAPI entries use their own connection. Other modes don't get these actions. A TEDAPI v1r entry uses a separate Tesla sign-in, saved as `/config/pypowerwall/.pypowerwall.auth`:
  - **Signing in:** on the v1r entry, choose ⋮ → Reconfigure → "Sign in to Tesla (for export windows)". The "TEDAPI v1r LAN (set up with Tesla account)" setup also saves one if none exists yet.
  - **Keeping it working:** Tesla only refreshes a sign-in whose token has been used within about 8 hours of signing in, and each refresh replaces the token in the file (pypowerwall's [cloud sign-in notes](https://github.com/jasonacox/pypowerwall/blob/v0.25.0/pypowerwall/cloud/AUTH.md)). The integration uses it a minute after loading and then every 6 hours. If Tesla stops accepting it, a repair appears under Settings → System → Repairs asking you to sign in again.
  - **One copy only:** don't copy the file elsewhere or point another tool at it. Whichever copy refreshes first leaves the others with a token Tesla no longer accepts.

```yaml
# Octopus Power Down event, 13:00-14:00 tomorrow
action: pypowerwall.set_export_window
data:
  start: "2026-11-28 13:00:00"
  end: "2026-11-28 14:00:00"
  price: 2.0
```

### Automation examples

[`docs/automations.md`](./docs/automations.md) has ready-to-paste automations for common cases: charging from the grid in a cheap time window, stopping export at negative prices, max backup during a weather warning, and a grid outage notification.

## Development

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for a fuller walkthrough of the dev setup, test architecture, and commit/PR conventions.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

python3 -m pytest
```

`requirements-dev.txt` adds `pytest` and `pytest-homeassistant-custom-component` (a full Home Assistant core install used only for testing) on top of `requirements.txt`.

### Releases

Versions are cut automatically by [release-please](https://github.com/googleapis/release-please) from [Conventional Commits](https://www.conventionalcommits.org/) on `main` (`feat:` → minor, `fix:` → patch, `feat!:`/`BREAKING CHANGE:` → major). It keeps a running release PR up to date with the changelog and version bump; merging that PR tags the release and updates `manifest.json` automatically. Commits that don't follow the convention don't trigger a release.

## Project structure

```text
custom_components/pypowerwall/
├── manifest.json         # HA integration metadata, pypowerwall dependency
├── const.py
├── config_flow.py        # connection-type menu + one form per mode, and options (scan interval)
├── coordinator.py         # DataUpdateCoordinator polling pypowerwall.Powerwall; builds Powerwall() kwargs per mode
├── entity.py               # shared device_info base entity
├── sensor.py
├── binary_sensor.py
├── number.py               # battery reserve control
├── select.py                # battery mode control, grid export control (Cloud/FleetAPI/v1r only)
├── switch.py                # grid charging control (Cloud/FleetAPI/v1r only)
├── button.py                # go off-grid / reconnect to grid (disconnect button disabled by default)
├── services.yaml            # schemas for max backup (v1r only) and export window actions
├── tariff.py                # export window: tariff rewrite and Tesla read/write
├── tariff_control.py        # export window: saved tariff, timed restore, retry
├── brand/                 # icon.png / icon@2x.png (HA 2026.3+ reads this directly, no brands-repo PR needed)
├── translations/         # en.json + one file per HA-supported language; no strings.json (Core-only build artifact, unused by custom integrations)
hacs.json
tests/
├── conftest.py            # make_fake_pw() stub + enable_custom_integrations fixture
├── test_coordinator.py    # pure unit tests: pypowerwall -> PowerwallData mapping, per-mode kwargs building
├── test_config_flow.py    # menu navigation + one success test per connection type
├── test_controls.py       # write-action entities/services driven through real HA service calls
└── test_setup.py          # full entry setup/unload against a stubbed Powerwall client
```

## License

MIT — see [LICENSE](./LICENSE).
