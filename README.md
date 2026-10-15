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

The integration requires the [`pyalso`](https://github.com/frankrosano/pyalso) library with its live-update extra. `requirements` in `manifest.json` points at the tagged GitHub release archive (`v0.2.2`), so Home Assistant installs it, and `grpcio`, automatically on first start.

Requires **Home Assistant 2025.12.0 or newer** (declared in `hacs.json`). That floor was determined by checking the Home Assistant sources: `UpdateFailed(retry_after=...)` first appears in 2025.12.0 (it is absent from 2025.11.0) and every other core API the integration uses exists there too. The suite itself only runs against the version pinned by the dev tooling (2026.2.3); 2025.12.0 has not been run.

## Set up

**Settings > Devices & services > Add integration > ALSO.** Enter the e-mail of your ALSO account. ALSO e-mails you a one-time code; enter it to finish. There is no password. Accounts that only use Google or Apple sign-in are not supported yet.

If the session ever expires, Home Assistant starts a re-authentication: you get a new code by e-mail and the same entry is updated. Both account identifiers the API exposes (user uuid and user reference id) are stored when the entry is created, and a re-authentication is accepted when the account matches EITHER of them, so it keeps working if the API later exposes a different set of ids.

**How sign-in tokens are kept.** The refresh token rotates on every refresh. Each rotated set is written to the config entry before the new access token is used, and Home Assistant saves config entries to disk about a second later (and once more when it stops). So there is a short window, about one second after a rotation, in which a crash or power loss leaves the previous (already spent) refresh token on disk and the next start asks you to sign in again. If updating the entry fails outright, the integration keeps working with the tokens in memory, logs an error and shows a repair issue until a later update succeeds; a restart before that also needs a new sign-in. A failure of the disk write itself is logged by Home Assistant and cannot be reported back to the integration.

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
| Charge state | Enum sensor from the push connection: `not_charging` or `fully_charged` (the two values checked against the real bike). Any other number the bike reports shows as `unknown_<n>` ("Unknown (n)"; `unknown_0`, `unknown_2` and `unknown_4` are the schema's other numbers and are always listed, any further number appears in the option list only while it is the current state and is not translated). Unknown until the first battery update arrives. Attribute `charging_state_raw` |
| Battery health | %, from the push connection (unit assumed, see below). Diagnostic |
| Firmware version | Text, from the push connection. Diagnostic. The bike's model name and firmware also appear on the device page |
| Charge state (shadow, raw) | Diagnostic, the plain integer `con_NChargeState` from the REST data. It is **not** the same enum as the push charge state (unplugged and idle read 0 here but `not_charging` = 1 there), so it is a separate sensor with no labels |
| Power state / Ride mode (raw) | Diagnostic, plain integers from the REST shadow (`con_NBikePowerState`, `hu_NRideModeREQ`). The push connection has values with the same-looking names, but they come from different enums; they are only available as the auto-discovered "(stream)" sensors below |
| Active faults | Count; the attribute `fault_level_raw` holds the raw level |
| GPS satellites | Diagnostic |
| Last location update | Diagnostic timestamp of the newest location, whether it arrived over the push connection or by polling |
| Connected | Connectivity binary sensor; attribute `disconnect_reason` |
| Push connected | Diagnostic, disabled by default. Whether the live connection is up (the raw transport state; it can be on while no data arrives); attributes `last_push`, `last_status`, `failed_attempts`, `polling_only` |
| Location | Device tracker (GPS). Attribute `altitude_raw`, passed on as reported (probably millimetres, an inference). Home Assistant always adds a `gps_accuracy` of 0 for trackers; the API's accuracy value is deliberately not used because its unit has never been verified |

### Raw and unverified values

The state-of-charge values, the position, the odometer (miles) and the push charge states 1 (not charging) and 3 (fully charged) have been checked against the real bike. Everything else is passed through as reported. The meaning of the other push charge states (2 and 4 come from the public schema only), the REST `con_NChargeState` numbers (only 0 = idle and unplugged, 3 = plugged in and charged were observed), the power, ride-mode and fault-level numbers is **not** known, so no labels are invented. Altitude is passed on raw (it looks like millimetres but is not verified). The units of "Time until fully charged" (minutes) and "Battery health" (percent) are assumed from the field names, not verified; a battery update without a time-to-full value is read as 0 (full).

### Auto-discovered sensors
The API returns more fields than the app shows, and may add more over time. Any numeric or boolean value in the bike's status data that has no dedicated entity becomes a **diagnostic sensor that is disabled by default**, named after the raw key (booleans show as 0/1). Enable the ones you want to watch. Nested objects, lists and text values are skipped, and position-like keys are never turned into sensors.

The same applies to values that **only the push connection delivers** (speed, predicted range, average power, assist level, gear, drive and ride mode, power state, OTA flag, ...): each numeric or boolean one becomes a disabled diagnostic sensor named `<key> (stream)`, with its own unique-id namespace (`<bike>_push_<key>`). Their units have never been observed, so they are raw: no unit, no device class. Values that already have an entity (battery level, charge limit, time until full, health, charge state) are not duplicated.

## Diagnostics and privacy

**Download diagnostics** on the integration page returns the complete raw status, location, connectivity, vehicle and push data for each bike, plus a rolling list of the last 20 distinct stream packets or fields that pyalso could not decode (a repeat is counted, not added again). Values are masked on a best-effort basis: text, large numbers (the size of nanodegree coordinates and bigger) and every 32-bit and 64-bit fixed-width value (so a float coordinate cannot survive) are blanked, undecodable payloads keep only feature, command, status and length; small integers and the field numbers are kept on purpose so new data can be recognised. Unknown shadow fields are kept, but any value that looks like an e-mail address, account/vehicle id, UUID, token, serial number or coordinate is redacted under whatever key it appears. Look at the file before sharing it anyway. This is the best way to share a captured charging or riding state. The push list also shows the push and the REST charge state raw numbers side by side. Your e-mail, tokens, serial numbers (bike and battery), coordinates, altitude, names and account/vehicle ids are redacted. Unknown fields are intentionally left visible. Have a look at the file before posting it publicly.

Raw payloads are never put on entity state attributes.

## Live updates (push)

After the first successful refresh the integration opens one read-only gRPC stream per bike and applies every message immediately, so battery, charging, location and odometer changes show up without waiting for the poll. The poll keeps running as the slower companion. Every value carries the time its source produced it and the newest one wins: an old cached message from the cloud never overwrites newer data, and a poll never discards newer pushed data.

If the stream cannot be used (`grpcio` is missing, or it keeps failing) the integration keeps working on polling alone and logs one warning per outage. If the stream's login is rejected, Home Assistant starts re-authentication. Entities stay available while a poll fails as long as the push connection DELIVERED DATA in the last seven minutes (a connection that is merely up, with only keep-alive replies, does not count), and the other way round; a timer flips them to unavailable at that deadline even if nothing else happens. If a stream gives up (login rejected, internal error) it is released and that bike stays on polling until the entry is reloaded or Home Assistant restarts; diagnostics then show `polling_only` and `push_enabled: false`. Streams are closed when the entry is unloaded or Home Assistant stops. Nothing is ever sent to the bike except keep-alives and read-only queries.

## Known limitations

- Push needs `grpcio` (installed through the `pyalso[push]` requirement). Without it the integration polls every polling interval.
- **`pyalso` is installed from a GitHub release archive, not from PyPI.** Home Assistant cannot version-check a requirement that is a URL, so it runs a pip install on every start and every reload of the integration, and the integration cannot load while GitHub is unreachable (for example when Home Assistant boots offline; this includes the restart right after installing from HACS). Publishing `pyalso` to PyPI and pinning `pyalso[push]==<version>` is the planned fix; until then this is a known cost.
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

Most tests mock the network layer of `pyalso` (its own `AlsoClient` merge code still runs) and use a fake push transport that is fed events by hand; `tests/test_contract.py` runs the real `AlsoAuth` and `AlsoClient` against a local fake HTTP service on 127.0.0.1 (token rotation, a failing storage write, a poll/push race). No real ALSO host and no real `grpcio` stream is used, and all data in `tests/` is invented. When running Home Assistant against the local library, install it into Home Assistant's environment (`uv pip install -e "/path/to/pyalso[push]" --python <ha-venv>/bin/python`); Home Assistant would otherwise install the tagged GitHub release of `pyalso` named in `manifest.json`. The latest gate results are in `docs/GATES.md`.
