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

The integration requires the [`pyalso`](https://github.com/frankrosano/pyalso) library with its live-update extra. `requirements` in `manifest.json` points at the tagged GitHub release archive (`v0.3.0`), so Home Assistant installs it, and `grpcio`, automatically on first start.

Requires **Home Assistant 2025.12.0 or newer** (declared in `hacs.json`). That floor was determined by checking the Home Assistant sources: `UpdateFailed(retry_after=...)` first appears in 2025.12.0 (it is absent from 2025.11.0) and every other core API the integration uses exists there too. The suite itself only runs against the version pinned by the dev tooling (2026.2.3); 2025.12.0 has not been run.

## Set up

**Settings > Devices & services > Add integration > ALSO.** Enter the e-mail of your ALSO account. ALSO e-mails you a one-time code; enter it to finish. There is no password. Accounts that only use Google or Apple sign-in are not supported yet.

If the session ever expires, Home Assistant starts a re-authentication: you get a new code by e-mail and the same entry is updated. Both account identifiers the API exposes (user uuid and user reference id) are stored when the entry is created, and a re-authentication is accepted when the account matches EITHER of them, so it keeps working if the API later exposes a different set of ids.

**How sign-in tokens are kept.** The refresh token rotates on every refresh. Each rotated set is written to the config entry before the new access token is used, and Home Assistant saves config entries to disk about a second later (and once more when it stops). So there is a short window, about one second after a rotation, in which a crash or power loss leaves the previous (already spent) refresh token on disk and the next start asks you to sign in again. If updating the entry fails outright, the integration keeps working with the tokens in memory, logs an error and shows a repair issue until a later update succeeds; a restart before that also needs a new sign-in. A failure of the disk write itself is logged by Home Assistant and cannot be reported back to the integration.

### Options

- **Polling interval**: default 300 s, minimum 60 s, maximum 3600 s. Live changes arrive over the push connection (see below); the poll only refreshes values the connection does not carry. The API is rate limited, so avoid very short intervals.

## Entities

Each bike is one device (manufacturer ALSO, model and serial number from the account). Every entity is enabled by default.

A value that both the push connection and the REST data report is **one** entity: it shows the push value while the push connection is up and has delivered that value since it connected, otherwise the REST value. These entities (marked "merged" below) have the attributes `source` (`stream` or `rest`), `stream_raw` and `rest_raw` (both raw values).

| Entity | Notes |
| --- | --- |
| Battery level | %, merged (displayed state of charge) |
| Charge limit | %, merged |
| Battery level (real) | Diagnostic, REST only |
| Odometer | Distance in miles (verified against the stream), merged; a push value lower than the REST one is ignored. Home Assistant converts it for your unit system. Total increasing |
| Time until fully charged | Minutes, from the push connection (unit assumed, see below). Unknown until the first battery update arrives, and unknown whenever the push connection leaves the value out (observed while charging) |
| Charge state | Enum sensor, merged: `not_charging`, `charging` or `fully_charged` (the three values checked against the real bike). The REST number uses a different numbering; only its observed values (0 = not charging, 2 = charging, 3 = fully charged) are translated, any other REST number is unknown. Any other push number shows as `unknown_<n>` ("Unknown (n)"; `unknown_0` and `unknown_4` are the schema's other numbers and are always listed, any further number appears in the option list only while it is the current state and is not translated) |
| Battery health | %, from the push connection (unit assumed, see below). Diagnostic |
| Firmware version | Text, from the push connection. Diagnostic. The bike's model name and firmware also appear on the device page |
| Ride mode | Enum sensor from the REST shadow (`hu_NRideModeREQ`), named like the app's ride modes: `sport`, `sport_manual`, `trail_manual`, `all_purpose`, `conserve`, `trail_cadence`, `custom`, `all_purpose_manual`. Sport and All purpose are confirmed on the bike and All purpose manual was observed; the others are the app's names for modes the bike does not offer yet. 0 (none selected, while asleep or parked) and any other number show as `unknown_<n>`. Attribute `rest_raw` (the number) |
| Power state | Diagnostic enum sensor from the REST shadow (`con_NBikePowerState`): `asleep` (3), `awake_battery_off` ("Awake (main battery off)", 2: woken but locked, or at power-off) and `battery_on` ("Main battery on", 4: unlocked/ready, riding or charging). Other numbers show as `unknown_<n>`. Attributes `rest_raw` and `candidate_name` (the app's name for the number, for example `OFF` for 1; unverified for numbers other than 2, 3 and 4) |
| Display knob | Diagnostic enum sensor from the REST shadow (`hu_NModeSelectorKnob`); the bike's display rotates and acts as the knob: `display_off` (0), `locked` (1, display on), `ready` (2, walk assist works here with the throttle), `ride` (3). Other numbers show as `unknown_<n>`. Attribute `rest_raw` |
| BMS state | Diagnostic enum sensor from the REST shadow (`bms_NState`): `standby` (2) or `active` (4, main battery connected). 5 (seen once, at power-off) and other numbers show as `unknown_<n>`. Attribute `rest_raw` |
| Active faults | Count; attributes `fault_level_raw` (the raw level) and `fault_codes` (each active fault code mapped to the ISO time it was set; `{}` when none is active) |
| GPS satellites | Diagnostic, merged |
| Last location update | Diagnostic timestamp of the location fix, merged (push fix time, else the REST location time) |
| Speed | Speed in mph (verified against the bike's display on a ride), merged (push GPS velocity, else REST `pcm_vVehicleFilt`, which is a whole number and a few seconds behind). Home Assistant converts it for your unit system. A stopped bike's push fixes leave the velocity out; that is shown as 0 from the push connection |
| Connected | Connectivity binary sensor; attributes `disconnect_reason` (the raw server text: `NONE` while connected, `MQTT_KEEP_ALIVE_TIMEOUT` in deep sleep, `CLIENT_INITIATED_DISCONNECT` after a restart) and `changed_at` (when the connection state last changed) |
| Push connected | Diagnostic. Whether the live connection is up (the raw transport state; it can be on while no data arrives); attributes `last_push`, `last_status`, `failed_attempts`, `polling_only` |
| Location | Device tracker (GPS), merged position (latitude and longitude always from one source). Attributes `source` and `altitude_raw` (passed on as reported, probably millimetres, an inference); the raw coordinates are not attributes. Home Assistant always adds a `gps_accuracy` of 0 for trackers; the API's accuracy value is deliberately not used because its unit has never been verified |

Upgrading from 0.3.x: five entities are replaced and their registry entries are deleted on setup (also when you had disabled them). Automations, dashboards and history that use them need the new entity:

| Removed unique id | Replaced by |
| --- | --- |
| `<bike>_ride_mode_raw` | Ride mode (`<bike>_ride_mode`) |
| `<bike>_power_state_raw` | Power state (`<bike>_power_state`) |
| `<bike>_speed_raw` | Speed (`<bike>_speed`, now mph with a unit and no longer diagnostic) |
| `<bike>_shadow_hu_NModeSelectorKnob` | Display knob (`<bike>_knob_position`) |
| `<bike>_shadow_bms_NState` | BMS state (`<bike>_bms_state`) |

The push connection's own `power_state_raw (stream)` and `ride_mode_raw (stream)` sensors are different values and are kept.

Upgrading from 0.2.x: the "Charge state (shadow, raw)" sensor and the auto-discovered `pcm_vVehicleFilt` and `velocity_raw (stream)` sensors are removed (their values are part of Charge state and Speed) and deleted from the entity registry on setup. Entities the integration used to disable by default are enabled on setup; entities you disabled yourself stay disabled, and nothing is enabled while the entry's "Enable newly added entities" option is off.

### Raw and unverified values

The state-of-charge values, the position, the odometer (miles) and the push charge states 1 (not charging), 2 (charging) and 3 (fully charged) have been checked against the real bike, as have the REST `con_NChargeState` numbers 0 (idle and unplugged), 2 (charging) and 3 (plugged in and charged). A live session on 2026-11-02 matched more values: speed is mph on both sources; REST power state 3 (asleep), 2 (awake, main battery off) and 4 (main battery on); the display knob 0 to 3; BMS state 2 (standby) and 4 (active); ride modes 1 (Sport), 4 (All purpose) and 8 (All purpose manual). The other ride-mode names (2, 3, 5, 6, 7) are taken from the app and not verified, because the bike only offers Sport and All purpose today. Everything else is passed through as reported. The meaning of the other push charge states (0 and 4 come from the public schema only), other REST charge-state numbers, the other power-state numbers, BMS state 5, the fault level and the fault codes is **not** known, so no labels are invented (`unknown_<n>`, or the raw number). Altitude is passed on raw (it looks like millimetres but is not verified). The units of "Time until fully charged" (minutes) and "Battery health" (percent) are assumed from the field names, not verified; a battery update without a time-to-full value leaves it unknown.

### Auto-discovered sensors
The API returns more fields than the app shows, and may add more over time. Any numeric or boolean value in the bike's status data that has no dedicated entity becomes a **diagnostic sensor** (enabled by default), named after the raw key (booleans show as 0/1). Nested objects, lists and text values are skipped, and position-like keys are never turned into sensors.

The same applies to values that **only the push connection delivers** (predicted range, average power, assist level, gear, drive and ride mode, power state, OTA flag, ...): each numeric or boolean one becomes a diagnostic sensor named `<key> (stream)`, with its own unique-id namespace (`<bike>_push_<key>`). Their units have never been observed, so they are raw: no unit, no device class. Values that already have an entity (battery level, charge limit, odometer, time until full, health, charge state, satellites, speed, location) are not duplicated. The REST keys `bms_NState` and `hu_NModeSelectorKnob` have their own entities (BMS state, Display knob) since 0.4.0 and are no longer auto-discovered.

## Diagnostics and privacy

**Download diagnostics** on the integration page returns the complete raw status, location, connectivity, vehicle and push data for each bike, plus a rolling list of the last 20 distinct stream packets or fields that pyalso could not decode (a repeat is counted, not added again). Values are masked on a best-effort basis: text, large numbers (the size of nanodegree coordinates and bigger) and every 32-bit and 64-bit fixed-width value (so a float coordinate cannot survive) are blanked, undecodable payloads keep only feature, command, status and length; small integers and the field numbers are kept on purpose so new data can be recognised. Unknown shadow fields are kept, but any value that looks like an e-mail address, account/vehicle id, UUID, token, serial number or coordinate is redacted under whatever key it appears. Look at the file before sharing it anyway. This is the best way to share a captured charging or riding state. The push list also shows the push and the REST charge state raw numbers side by side, whether pyalso treats the stream as connected (`stream_connected`), and which source each merged value currently uses (`sources`). Your e-mail, tokens, serial numbers (bike and battery), coordinates, altitude, names and account/vehicle ids are redacted. Unknown fields are intentionally left visible. Have a look at the file before posting it publicly.

Raw payloads are never put on entity state attributes.

## Live updates (push)

After the first successful refresh the integration opens one read-only gRPC stream per bike and applies every message immediately, so battery, charging, location and odometer changes show up without waiting for the poll. The poll keeps running as the slower companion. For a value both report, the push value is shown while the stream is connected and has delivered it since it connected; otherwise (stream down, or it never sent that value) the REST value is the fallback. Within the push connection an old cached message never overwrites newer data, and a poll never discards pushed data.

If the stream cannot be used (`grpcio` is missing, or it keeps failing) the integration keeps working on polling alone and logs one warning per outage. If the stream's login is rejected, Home Assistant starts re-authentication. Entities stay available while a poll fails as long as the push connection DELIVERED DATA in the last seven minutes (a connection that is merely up, with only keep-alive replies, does not count), and the other way round; a timer flips them to unavailable at that deadline even if nothing else happens. If a stream gives up (login rejected, internal error) it is released and that bike stays on polling until the entry is reloaded or Home Assistant restarts; diagnostics then show `polling_only` and `push_enabled: false`. Streams are closed when the entry is unloaded or Home Assistant stops. Nothing is ever sent to the bike except keep-alives and read-only queries.

## Known limitations

- Push needs `grpcio` (installed through the `pyalso[push]` requirement). Without it the integration polls every polling interval.
- **`pyalso` is installed from a GitHub release archive, not from PyPI.** Home Assistant cannot version-check a requirement that is a URL, so it runs a pip install on every start and every reload of the integration, and the integration cannot load while GitHub is unreachable (for example when Home Assistant boots offline; this includes the restart right after installing from HACS). Publishing `pyalso` to PyPI and pinning `pyalso[push]==<version>` is the planned fix; until then this is a known cost.
- Overnight behaviour (token expiry on a stream that stays open for hours) has not been observed yet; see `docs/STATUS.md`. During a ride the push connection sent only location updates (no ride, energy or lock data).
- Read only: no remote commands.
- Google and Apple sign-in are not supported.

## Branding
The ALSO name, logo and app icon are trademarks and property of ALSO (the e-bike maker). The images in `custom_components/also/brand/` were taken from the official ALSO mobile app and are used only to identify this integration in Home Assistant (2026.3 and newer). This project is not affiliated with or endorsed by ALSO. The images will be removed on request.

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
