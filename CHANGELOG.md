# Changelog

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
