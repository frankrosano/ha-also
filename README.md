# ALSO for Home Assistant

A custom integration that adds your ALSO account and shows data about your ALSO e-bike in Home Assistant.

> **Unofficial.** This project is not affiliated with or endorsed by ALSO. It talks to the same cloud API the mobile apps use, which was worked out by reverse engineering. Use it only with your own account. The API can change or block access at any time, and there is no warranty.

## Install

### HACS (custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/frankrosano/ha-also` with the category **Integration**.
3. Install **ALSO**, then restart Home Assistant.

### Manual

Copy `custom_components/also/` into your Home Assistant `config/custom_components/` folder and restart.

The integration requires the [`pyalso`](https://github.com/frankrosano/pyalso) library with its live-update extra. `requirements` in `manifest.json` points at the tagged GitHub release archive (`v0.2.1`), so Home Assistant installs it, and `grpcio`, automatically on first start.

## Set up

**Settings > Devices & services > Add integration > ALSO.** Enter the e-mail of your ALSO account. ALSO e-mails you a one-time code; enter it to finish. There is no password. Accounts that only use Google or Apple sign-in are not supported yet.

If the session ever expires, Home Assistant starts a re-authentication: you get a new code by e-mail and the same entry is updated. The refresh token rotates, and every rotated token set is written back to the config entry.

### Options

- **Polling interval**: default 300 s, minimum 60 s, maximum 3600 s. Live changes arrive over the push connection (see below); the poll only refreshes values the connection does not carry. The API is rate limited, so avoid very short intervals.

## Entities

Each bike is one device (manufacturer ALSO, model and serial number from the account).

| Entity | Notes |
| --- | --- |
| Battery level | % (displayed state of charge) |
| Charge limit | % |
| Battery level (real) | Diagnostic, disabled by default |
| Odometer | Distance in miles (verified against the stream); Home Assistant converts it for your unit system. Total increasing |
| Time until fully charged | Minutes, from the push connection (unit assumed, see below). Unknown until the first battery update arrives |
| Charge state | Enum sensor from the push connection: `not_charging` or `fully_charged` (the two values checked against the real bike). Any other number the bike reports shows as `unknown_<n>` (for example `unknown_2` while charging, until that value is confirmed). Unknown until the first battery update arrives. Attribute `charging_state_raw` |
| Battery health | %, from the push connection (unit assumed, see below). Diagnostic |
| Firmware version | Text, from the push connection. Diagnostic. The bike's model name and firmware also appear on the device page |
| Charge state (shadow, raw) | Diagnostic, the plain integer `con_NChargeState` from the REST data. It is **not** the same enum as the push charge state (unplugged and idle read 0 here but `not_charging` = 1 there), so it is a separate sensor with no labels |
| Power state / Ride mode (raw) | Diagnostic, plain integers |
| Active faults | Count; the attribute `fault_level_raw` holds the raw level |
| GPS satellites | Diagnostic |
| Last location update | Diagnostic timestamp of the newest location, whether it arrived over the push connection or by polling |
| Connected | Connectivity binary sensor; attribute `disconnect_reason` |
| Push connected | Diagnostic, disabled by default. Whether the live connection is up; attributes `last_push`, `last_status`, `failed_attempts`, `polling_only` |
| Location | Device tracker (GPS). Attribute `altitude_raw`, passed on as reported (probably millimetres, not verified). Home Assistant always adds a `gps_accuracy` of 0 for trackers; the API accuracy is not used |

### Raw and unverified values

The state-of-charge values, the position, the odometer (miles) and the push charge states 1 (not charging) and 3 (fully charged) have been checked against the real bike. Everything else is passed through as reported. The meaning of the other push charge states (2 and 4 come from the public schema only), the REST `con_NChargeState` numbers (only 0 = idle and unplugged, 3 = plugged in and charged were observed), the power, ride-mode and fault-level numbers is **not** known, so no labels are invented. Altitude is passed on raw (it looks like millimetres but is not verified). The units of "Time until fully charged" (minutes) and "Battery health" (percent) are assumed from the field names, not verified; a battery update without a time-to-full value is read as 0 (full).

### Auto-discovered sensors

The API returns more fields than the app shows, and may add more over time. Any numeric or boolean value in the bike's status data that has no dedicated entity becomes a **diagnostic sensor that is disabled by default**, named after the raw key (booleans show as 0/1). Enable the ones you want to watch. Nested objects, lists and text values are skipped, and position-like keys are never turned into sensors.

## Diagnostics and privacy

**Download diagnostics** on the integration page returns the complete raw status, location, connectivity, vehicle and push data for each bike, plus a rolling list of the last 20 distinct stream packets or fields that pyalso could not decode (a repeat is counted, not added again). Values are masked on a best-effort basis: text and large numbers (the size of nanodegree coordinates and bigger) are blanked, undecodable payloads keep only feature, command, status and length; small numbers and 32-bit floats are kept on purpose so new data can be recognised, so look at the file before sharing it. This is the best way to share a captured charging or riding state. The push list also shows the push and the REST charge state raw numbers side by side. Your e-mail, tokens, serial numbers (bike and battery), coordinates, altitude, names and account/vehicle ids are redacted. Unknown fields are intentionally left visible. Have a look at the file before posting it publicly.

Raw payloads are never put on entity state attributes.

## Live updates (push)

After the first successful refresh the integration opens one read-only gRPC stream per bike and applies every message immediately, so battery, charging, location and odometer changes show up without waiting for the poll. The poll keeps running as the slower companion. Every value carries the time its source produced it and the newest one wins: an old cached message from the cloud never overwrites newer data, and a poll never discards newer pushed data.

If the stream cannot be used (`grpcio` is missing, or it keeps failing) the integration keeps working on polling alone and logs one warning per outage. If the stream's login is rejected, Home Assistant starts re-authentication. Entities stay available while a poll fails as long as the push connection is up (or delivered data in the last seven minutes), and the other way round. Streams are closed when the entry is unloaded or Home Assistant stops. Nothing is ever sent to the bike except keep-alives and read-only queries.

## Known limitations

- Push needs `grpcio` (installed through the `pyalso[push]` requirement). Without it the integration polls every polling interval.
- Overnight behaviour (token expiry on a stream that stays open for hours) and ride-time data have not been observed yet; see `docs/STATUS.md`.
- Read only: no remote commands.
- Google and Apple sign-in are not supported.

## Development

Needs Python 3.13.2 or newer and [uv](https://docs.astral.sh/uv/).

```bash
cd ha-also
uv venv                                   # resolved to Python 3.13 on this machine
uv pip install -e "../pyalso[push]"        # local library with the push extra (not on PyPI yet)
uv pip install -e ".[dev]"
uv run ruff check
uv run ruff format --check
uv run pytest
```

Note on the Python version: the dev pins (`pytest-homeassistant-custom-component==0.13.316`, which pins `homeassistant==2026.2.3`) were installed and tested on CPython 3.13 here. Newer releases of that test package need Python 3.14.

Tests mock `pyalso` and use a fake push transport that is fed events by hand; no network and no real `grpcio` stream is used, and all data in `tests/` is invented. When running Home Assistant against the local library, install it into Home Assistant's environment (`uv pip install -e "/path/to/pyalso[push]" --python <ha-venv>/bin/python`); Home Assistant would otherwise install the tagged GitHub release of `pyalso` named in `manifest.json`. The latest gate results are in `docs/GATES.md`.
