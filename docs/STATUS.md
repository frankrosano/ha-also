# ALSO integration: status and handoff

Both projects are scaffolded and have no commits yet. pyalso 0.2.1 and ha-also 0.2.1 carry the live push stream. Nothing has been tested against a riding bike or against a stream that stays open for hours. Live checks so far were made with the bike plugged in and charged to its 90% limit, and later unplugged and idle. Gate results (ruff, ruff format, pytest) are recorded verbatim in `docs/GATES.md`.

## 1. What exists

### pyalso (`<path-to-pyalso>`, v0.2.1)
Read-only async client plus the push transport. See its README and CHANGELOG.

- REST: e-mail OTP login with rotating refresh tokens, user, vehicles, status shadow, location, connectivity.
- Push: `AlsoStream` (optional `grpcio`, `pip install "pyalso[push]"`), tolerant event decoders, `updates_from_event`, per-field source times so the newest data wins (`VehicleState.merge_update`, `AlsoClient.apply_push_update`).
- Version 0.2.1 is consistent in `pyproject.toml`, `const.py` (`__version__` fallback and `User-Agent`), the README and the CHANGELOG.

### ha-also (`<path-to-ha-also>`, v0.2.1, domain `also`, `iot_class: cloud_push`)
- Config flow: e-mail then OTP, reauth (aborts with `wrong_account` for another account), options flow with the polling interval (default 300 s, 60 to 3600).
- Tokens: every rotation is written to the config entry. Setup treats `ValueError` and `TypeError` from `Tokens.from_dict` as a reauth case.
- Coordinator (`coordinator.py`):
  - REST poll as before (slower companion now).
  - One `AlsoStream` per bike, started after the first successful refresh and after the platforms exist. Bikes that appear or disappear on a later vehicle-list refresh get their stream started or stopped.
  - Every decoded event goes through `updates_from_event` into `async_apply_push_update`, which calls `AlsoClient.apply_push_update` (pyalso merges, newest source time wins) and notifies the entities. It does NOT reset the poll timer, so a busy stream cannot starve the poll.
  - Connection state per bike (`PushStatus`), a diagnostic binary sensor, and a rolling window of the last 20 DISTINCT unknown packets/fields (a repeat of the same kind/feature/command/status/field numbers bumps a `count` instead of adding an entry, so one recurring field cannot evict the rare ones).
  - Fallback: `grpcio` missing or the stream failing to start disables push for the entry (one warning). Three failed attempts in a row log ONE warning per outage and PER BIKE (re-armed when that bike's stream connects again); the stream keeps retrying by itself. pyalso logs a failed stream open once per outage too, then at debug.
  - Availability follows either source: an entity is available when the last REST poll worked OR the bike's push stream is connected / delivered data within 420 s (3 push cadences of 120 s plus 60 s margin, `PUSH_FRESH_SECONDS`). A failing poll no longer blanks live data. Home Assistant itself logs a failing poll once.
  - Fatal auth error from the stream: `ConfigEntry.async_start_reauth`. Entities stay available because REST polling may still work.
  - Streams stop on unload (`async_shutdown` plus an explicit stop in `async_unload_entry`) and on `EVENT_HOMEASSISTANT_STOP`.
  - The device's model and software version are updated in the device registry when the stream reports them.
- Entities per bike:
  - sensors: battery level, charge limit, real battery level (disabled), odometer (miles, distance, total increasing), time until fully charged (min, push), battery health (%, push, diagnostic), firmware version (push, diagnostic), charge state (enum from the PUSH stream: `not_charging` / `fully_charged` / `unknown_<n>`), charge state (shadow, raw) (diagnostic, REST `con_NChargeState`), power state (raw), ride mode (raw), fault count, GPS satellites, last location update (newest of the pushed fix and the REST location)
  - binary sensors: connected, push connected (diagnostic, disabled by default; attributes `last_push`, `last_status`, `failed_attempts`, `polling_only`)
  - device tracker: location (attribute `altitude_raw`, probably millimetres, not verified)
  - auto-discovered disabled diagnostic sensors for any other numeric or boolean shadow key
- Diagnostics: redacted (identifiers, tokens, coordinates, altitude, serials, bike/battery serials from the stream) plus push state and the unknown packet window.
- Tests: see `docs/GATES.md` for the count and the verbatim output. They use a fake transport and the real pyalso merge and decoders. No `grpcio` is needed.

### Charge-state finding (0.2.1)

The stream's `BatteryState.charging_state` and the REST shadow's `con_NChargeState` are DIFFERENT enums. Same bike, same minutes: unplugged and idle at 89% (limit 90%) gave stream 1 (`NOT_CHARGING`) and REST 0; plugged in and charged at the limit gave stream 3 (`FULLY_CHARGED`) and REST 3. The match at 3 was a coincidence, and 0.2.0 wrongly showed the stream value under the REST key. Now:

- "Charge state" is an enum sensor driven ONLY by the push stream (pyalso `VehicleState.charging_state`). Options `not_charging` (1) and `fully_charged` (3), the two values confirmed on the bike. Any other number is `unknown_<n>` (the sensor's options list grows by that value so Home Assistant never raises). The schema's CHARGING (2) and CHARGING_ISSUE (4) are deliberately NOT named until observed, so a charging bike will currently read `unknown_2`. It is `unknown` until the first battery packet arrives; the REST value is never used as a fallback.
- "Charge state (shadow, raw)" is the REST `con_NChargeState` as a plain diagnostic integer, no labels (only 0 = idle/unplugged and 3 = plugged in and charged were observed).
- The old `charge_state_name` attribute is gone (it labelled a REST number with stream names). The unique id of the raw sensor is unchanged (`<vehicle>_charge_state_raw`), only its name changed; the enum sensor is new (`<vehicle>_charge_state`).
- Diagnostics list `charging_state_raw` and `shadow_charge_state_raw` side by side per bike.

### Other 0.2.1 changes

- Availability from either source, newest-location timestamp, per-bike outage logging and the de-duplicated unknown-packet window (described above).
- Privacy scrub: the real altitude and all machine-specific home-directory paths were removed from shipped files and docs, fake ids are `VHC-AAAA-BBBB-CCCC` style, fake coordinates are within 10 degrees of 0,0, `scripts/check_no_pii.py` (also run as `tests/test_no_pii.py`) fails on e-mails, reference ids, serial-like strings, UUIDs, coordinates, tokens and home paths. `.agents/` (planning artefacts, which still contain developer-local paths) is git-ignored. The scan result is in `docs/GATES.md`.

### Decisions worth a second look
- The odometer sensor key changed from `odometer_raw` to `odometer` (name no longer says "unit unverified"). A registry migration keeps the history of an existing `..._odometer_raw` entity; its entity id is not renamed.
- The poll interval is NOT shortened when push is unavailable; the configured value applies (default 300 s). Say if you want a faster fallback.
- Unknown packets are masked before they reach diagnostics (`unknown_packets.py`): printable text becomes `x`, varints of 100 000 000 or more and every fixed64 become 0, nested messages are masked recursively, undecodable payloads keep only feature/command/status/length. The log line has no payload. Fixed32 values and small numbers are kept on purpose; they are what we want to learn from.
- `unknown_packets.py` imports pyalso's private `_wire` module (pinned version). A public re-export in pyalso would be cleaner.
- Battery serial is not exposed as an entity (privacy); it only exists in the push state and is redacted in diagnostics.
- The diagnostics redaction now also matches short position words (`lat`, `lon`, `gps`, ...) as whole words, and secret-looking names (`serial`, `token`, ...). Same hints skip such keys in auto-discovery. This closes the earlier position-hint findings.

## 2. Install both locally in a dev Home Assistant

The ha-also test venv uses Python 3.13 (`pytest-homeassistant-custom-component==0.13.316`), not 3.14. Use a 3.13 venv for HA too, unless your HA release supports 3.14.

```bash
# 1. Integration: symlink into the HA config dir
mkdir -p <ha-config>/custom_components
ln -s <path-to-ha-also>/custom_components/also <ha-config>/custom_components/also

# 2. Library with the push extra: install editable into the environment that runs HA
uv pip install -e "<path-to-pyalso>[push]" --python <path-to-ha-venv>/bin/python
```

`manifest.json` requires `pyalso[push]` from the tagged GitHub release archive (`v0.2.1`), not PyPI. To develop against a local checkout instead, pre-install the editable copy (HA skips requirements that are already satisfied). Restart HA, then add the ALSO integration and enter the e-mail OTP code. The `Push connected` diagnostic sensor is disabled by default; enable it to watch the stream.

Run the gates with:

```bash
cd <path-to-ha-also>
uv run ruff check && uv run ruff format --check && uv run pytest
```

The `.venv` here has pyalso installed editable with the push extra (`uv pip install -e "<path-to-pyalso>[push]"`), currently with `grpcio` 1.84.0. HA core pins 1.83.1; pyalso asks for `grpcio>=1.60`.

## 3. Known unknowns

- Enum meanings: the push charge states 1 (not charging) and 3 (fully charged) are verified; 2 (charging) and 4 (issue) come from the public schema only. The REST `con_NChargeState` has its own numbering (only 0 and 3 observed, labels unknown). `bms_NState`, `con_NBikePowerState`, `hu_NRideModeREQ`, `hu_NModeSelectorKnob` and `NFaultLevel` stay raw ints.
- Altitude unit (looks like millimetres), velocity unit (mph per the public schema) and GPS accuracy are unverified.
- What the stream sends while riding or charging: `RIDE_INFO_CHANGE`, `ENERGY_STATE_CHANGE`, `FIRMWARE_VERSION_CHANGE` and `OTA_NOTIFICATION` decoders follow the public schema and were never seen on the wire. Their values reach the push state but no entity yet.
- `minutes_till_full` and `battery_health` units are assumed (minutes, percent). proto3 omits zero, so a stamped battery packet without `minutes_till_full` is read as 0 (full).
- Token expiry on an open stream: pyalso opens a successor stream before the token expires, but this was only tested with fakes. The real behaviour over hours is unknown.
- Whether other shadow names exist beyond `status` (not probed).
- Refresh failures: the invalid-grant mapping in pyalso is a guess; the real failure shape has not been observed.
- hassfest has not been run (no Home Assistant core checkout here).

## 4. Next steps

1. Stimulus experiments (you, with the bike). Enable the `Push connected` sensor and the disabled diagnostic sensors first, then download diagnostics after each step to capture the unknown packets:
   - charge with a raised limit (above 90%): the charge state sensor should show `unknown_2` (CHARGING per the schema); note what REST `con_NChargeState` reads while charging and how `minutes_till_full` behaves; then name those values;
   - unplug and replug the charger and watch how quickly the push charge state changes;
   - wake the bike (power state, firmware/ride info packets);
   - ride (location, velocity, ride/energy/drive packets, odometer; check the altitude and speed units);
   - leave Home Assistant running overnight and check that the stream survives the access-token expiry (watch `last_status` and `failed_attempts`, and the log for the single outage warning).
2. Flip the `fields.py` verified flags in pyalso and add entities (ride mode, power state, range) for what the experiments confirm; add `charging` / `charging_issue` to the charge state options once seen.
3. Optionally publish pyalso to PyPI and switch the manifest requirement from the GitHub release archive to a pinned PyPI version. Add brand images (`brand/` folder) and remove `ignore: brands` from the HACS workflow.
4. Run hassfest and a Python 3.14 check when the newer test package is wanted.
5. Open follow-ups: a test for a failing token persist and for a vehicle vanishing from the account (the push side is covered).
6. Design ideas, not done: tighten unknown-packet masking (small varints and fixed32 values are kept on purpose; a micro-degree or float32 coordinate in an unknown packet would be visible), and a public re-export of pyalso's wire reader instead of importing `_wire`.
