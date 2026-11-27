"""Constants for the pypowerwall integration."""

DOMAIN = "pypowerwall"

CONF_CONN_TYPE = "conn_type"
CONF_GW_PWD = "gw_pwd"
CONF_AUTHPATH = "authpath"
CONF_SITEID = "siteid"
CONF_RSA_KEY_PATH = "rsa_key_path"
CONF_WIFI_HOST = "wifi_host"

# Connection types, each mapping to a distinct pypowerwall.Powerwall() call shape.
# See coordinator.build_powerwall_kwargs() for exactly which kwargs each one sends.
CONN_TYPE_LOCAL = "local"
CONN_TYPE_TEDAPI = "tedapi"
CONN_TYPE_HYBRID = "hybrid"
CONN_TYPE_TEDAPI_V1R = "tedapi_v1r"
CONN_TYPE_CLOUD = "cloud"
CONN_TYPE_FLEETAPI = "fleetapi"

# Cloud and FleetAPI modes authenticate via a pre-existing token/config cache file
# (produced by `python -m pypowerwall setup` / `python -m pypowerwall.fleetapi setup`
# respectively) rather than credentials a form can collect directly; v1r LAN mode
# similarly needs a pre-registered RSA key file. These three all take a filesystem
# path rather than inline credentials.
FILE_BASED_CONN_TYPES = (CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI, CONN_TYPE_TEDAPI_V1R)

# The file pypowerwall reads from `authpath` in each mode. pypowerwall also writes
# refreshed tokens back into that directory, so it must be writable too.
AUTH_CACHE_FILENAMES = {
    CONN_TYPE_CLOUD: ".pypowerwall.auth",
    CONN_TYPE_FLEETAPI: ".pypowerwall.fleetapi",
}

# set_grid_charging()/get_grid_charging() and set_grid_export()/get_grid_export() are
# implemented by the Cloud and FleetAPI backends and, since pypowerwall 0.15.0, by the
# TEDAPI backend as a LAN config write over the signed v1r transport. pypowerwall's
# facade docstrings still say Cloud/FleetAPI only; they predate the v1r support. Plain
# TEDAPI/hybrid/local don't support it -- the TEDAPI setters log "requires v1r
# transport" and return None. Used to gate the switch.py and select.py entities that
# expose them, and the coordinator's polling.
GRID_CONTROL_CONN_TYPES = (CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI, CONN_TYPE_TEDAPI_V1R)

# Modes that talk to Tesla's rate-limited cloud API rather than a LAN gateway.
CLOUD_CONN_TYPES = (CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI)

# get_tariff() is likewise Cloud/FleetAPI only (TEDAPI returns an empty mock, local None).
TARIFF_CONN_TYPES = (CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI)

# go_off_grid()/reconnect_grid() are facade methods on pypowerwall.Powerwall that, as of
# 0.17.3, are only actually implemented by the TEDAPI backend's signed v1r transport
# (send_island_mode()) -- local/TEDAPI (non-v1r)/hybrid/cloud/FleetAPI backends still
# no-op. Used to gate the go_off_grid/reconnect_grid buttons in button.py.
GRID_ISLANDING_CONN_TYPES = (CONN_TYPE_TEDAPI_V1R,)

DEFAULT_SCAN_INTERVAL = 5
# Cloud/FleetAPI modes poll Tesla's rate-limited cloud API rather than a LAN
# gateway, so they default to a slower interval to avoid throttling; see
# CLOUD_CONN_TYPES for which connection types this applies to.
DEFAULT_SCAN_INTERVAL_CLOUD = 60
MIN_SCAN_INTERVAL = 2

# The gateway reports each pack's current full-charge capacity
# (nominal_full_pack_energy) but not its original design capacity, so battery
# health compares against this user-configurable per-pack figure instead.
# 13.5 kWh is Tesla's rated usable capacity for both Powerwall 2 and 3.
CONF_DESIGN_CAPACITY = "design_capacity_kwh"
DEFAULT_DESIGN_CAPACITY_KWH = 13.5

MANUFACTURER = "Tesla"

# pypowerwall.Powerwall()'s own default (5s) is tight for TEDAPI mode against a
# Powerwall 3 gateway: building the /api/meters/aggregates response there fans out
# into an extra internal get_pw3_vitals() request, so a single slow gateway/Wi-Fi
# response can blow the budget and surface as a spurious read-timeout UpdateFailed.
# Passed to every connection type in coordinator.build_powerwall_kwargs().
POWERWALL_REQUEST_TIMEOUT = 10

# A Cloud client only reads .pypowerwall.auth when it connects, so after a failed poll
# the coordinator rebuilds it from the file, at most this often (seconds), to pick up
# tokens rotated by another client on the same file or a re-run of pypowerwall setup.
CLOUD_RECONNECT_INTERVAL = 300

# Max backup (storm watch) actions -- momentary, not persistent state, so they're
# exposed as services (registered in __init__.py, v1r LAN mode only) rather than
# entities. See pypowerwall.Powerwall.schedule_max_backup()/cancel_max_backup().
SERVICE_SCHEDULE_MAX_BACKUP = "schedule_max_backup"
SERVICE_CANCEL_MAX_BACKUP = "cancel_max_backup"
ATTR_DURATION_SECONDS = "duration_seconds"

# Where the config flow's Tesla sign-in and v1r key go, under HA's config dir. The
# tariff manager also looks here for the sign-in a v1r entry uses for tariff writes.
CLOUD_LOGIN_AUTH_DIR = "pypowerwall"

# Tariff manager (tariff_manager.py): Home Assistant writes the site's Tesla tariff
# from the Octopus Energy integration's day-rate event entities, set in the options.
CONF_TARIFF_IMPORT_RATES = "tariff_import_rates_entity"
CONF_TARIFF_EXPORT_RATES = "tariff_export_rates_entity"
TARIFF_CONTROL_CONN_TYPES = (CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI, CONN_TYPE_TEDAPI_V1R)
SERVICE_SET_EXPORT_WINDOW = "set_export_window"
SERVICE_CLEAR_EXPORT_WINDOWS = "clear_export_windows"
SERVICE_UPDATE_TARIFF = "update_tariff"
ATTR_START = "start"
ATTR_END = "end"
ATTR_PRICE = "price"
DEFAULT_EXPORT_WINDOW_PRICE = 2.0
