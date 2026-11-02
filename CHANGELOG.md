# Changelog
## 0.4.0
Named states for the bike's ride mode, power state, display knob and BMS state, speed in mph, and
the active fault codes, from a live session on the bike (2026-11-02). Needs pyalso 0.4.0 (the
manifest requires the pyalso v0.4.0 release archive).

**Breaking:**
* Replaced entities, deleted from the entity registry on setup (also when you had disabled them).
  Automations, scripts, dashboards and history that use them break:
  * `<bike>_ride_mode_raw` -> `<bike>_ride_mode` (Ride mode, enum).
  * `<bike>_power_state_raw` -> `<bike>_power_state` (Power state, enum, diagnostic).
  * `<bike>_speed_raw` -> `<bike>_speed` (Speed): now mph with device class speed, converted by
    Home Assistant for your unit system, and no longer diagnostic.
  * `<bike>_shadow_hu_NModeSelectorKnob` (auto-discovered) -> `<bike>_knob_position` (Display
    knob, enum, diagnostic). `hu_NModeSelectorKnob` is no longer auto-discovered.
  * `<bike>_shadow_bms_NState` (auto-discovered) -> `<bike>_bms_state` (BMS state, enum,
    diagnostic). `bms_NState` is no longer auto-discovered.
  The stream's own `<bike>_push_power_state_raw` and `<bike>_push_ride_mode_raw` sensors are
  different values and are kept.

Added:
* Ride mode: `sport`, `sport_manual`, `trail_manual`, `all_purpose`, `conserve`, `trail_cadence`,
  `custom`, `all_purpose_manual` (the app's names; Sport and All purpose are confirmed on the bike,
  All purpose manual was observed, the others are not offered by the bike yet). 0 (none selected)
  and other numbers are `unknown_<n>`. Attribute `rest_raw`.
* Power state: `asleep`, `awake_battery_off` and `battery_on` (the verified numbers 3, 2 and 4);
  other numbers are `unknown_<n>`. Attributes `rest_raw` and `candidate_name` (the app's name).
* Display knob: `display_off`, `locked`, `ready`, `ride`; BMS state: `standby`, `active`. Other
  numbers are `unknown_<n>`. Attribute `rest_raw`.
* Every enum sensor lists an `unknown_<n>` value in its options only while it is the state (the
  charge-state behaviour, now shared).
* Active faults: attribute `fault_codes`, each active fault code mapped to the ISO time it was set.
* Connected: attribute `changed_at`, when the connection state last changed. `disconnect_reason`
  stays the raw server text.
* Speed comes from the stream as 0.0 when a stopped bike's location update leaves the velocity
  out, instead of keeping the last pushed speed or falling back to REST.
* Tests for the speed sensor's stream path (0.3.0 review finding HA-2).

## 0.3.0
One entity per value: a value that both the push stream and REST report is merged, with the stream
preferred while it is connected and has delivered the value, and REST as the fallback. Needs pyalso
0.3.0.

**Breaking:**
* Removed entities, deleted from the entity registry on setup. Automations, scripts and dashboards
  that use them break:
  * `<bike>_charge_state_raw` ("Charge state (shadow, raw)", REST `con_NChargeState`): now the
    `rest_raw` attribute of Charge state.
  * `<bike>_shadow_pcm_vVehicleFilt` (auto-discovered `pcm_vVehicleFilt`) and
    `<bike>_push_velocity_raw` (auto-discovered `velocity_raw (stream)`): now the new Speed (raw)
    sensor.
* Charge state: the attribute `charging_state_raw` is replaced by `source` / `stream_raw` /
  `rest_raw`. It now falls back to REST (0 = not charging, 2 = charging, 3 = fully charged; other
  REST numbers are unknown) instead of staying unknown without push data. The stream's 2 is the
  named option `charging` ("Charging"); `unknown_2` is gone.
* Time until fully charged is unknown (no longer 0) when the stream leaves the value out, as
  observed while charging.
* Battery level, charge limit, odometer, GPS satellites, last location update and the location
  tracker follow the stream-first rule instead of "the newest source time wins": while the stream is
  connected, a cached stream value beats a newer REST value.

Other changes:
* Every entity is enabled by default, including the auto-discovered "(stream)" and shadow sensors,
  Battery level (real) and Push connected. Entities the integration had disabled are re-enabled on
  setup; entities a user disabled are untouched, and nothing is re-enabled while the entry's
  "Enable newly added entities" system option is off.
* New Speed (raw) diagnostic sensor (stream GPS velocity, else REST `pcm_vVehicleFilt`; raw, no unit).
* Merged entities carry the attributes `source`, `stream_raw` and `rest_raw`; the tracker carries
  `source`.
* Diagnostics show `stream_connected` and the per-value `sources`.
* The manifest requires pyalso `v0.3.0`.
* Brand images: the integration ships its ALSO icon, wordmark logo and a white (dark mode) logo in
  `custom_components/also/brand/` (Home Assistant 2026.3 and newer shows them; older versions ignore them).
  The `ignore: brands` line is gone from the HACS validation workflow. See the README section Branding.

## 0.2.2

Hardening release after a two-model semantic review (`docs/REVIEW_RESPONSE.md`). Needs pyalso 0.2.2
and Home Assistant 2025.12.0 or newer.

* Availability needs recent push DATA (not a merely connected stream) and a per-bike timer flips
  entities at the freshness deadline.
* A fatal stream status now stops and releases the stream; the bike stays on polling and
  diagnostics say so. grpcio is imported off the event loop.
* Rotated tokens: a failing config-entry update no longer breaks the session; it is logged and
  shown as a repair issue. The README states exactly what is durable and when.
* A push that arrives during a multi-bike poll is no longer lost.
* Reauth matches the account by either stored identifier (uuid or reference id).
* Diagnostics: values that look like e-mail, ids, tokens, serials or coordinates are redacted under
  any key; fixed32 values and groups in unknown packets are masked.
* New disabled diagnostic sensors for stream-only values (`<key> (stream)`); `charge_limit` has a
  state class; the schema's other charge states are listed as neutral "Unknown (n)".
* Devices of bikes that left the account can be deleted.
* `hacs.json` declares the minimum Home Assistant version; the manifest requires pyalso `v0.2.2`.
  Known limitation: Home Assistant reinstalls a URL requirement on every start (README).
