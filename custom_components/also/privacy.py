"""Shared privacy heuristics: which payload keys might carry a position or a secret."""

from __future__ import annotations

import re

# Whole-word split of Hungarian-notation names: ``con_aLat`` -> ``con``, ``a``, ``Lat``.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

# Long names are matched anywhere in the key (``foo_aMysteryLatitude``).
POSITION_SUBSTRINGS = ("latitude", "longitude", "altitude", "lng")
# Short names are matched as whole words only, so ``platform`` or ``default`` stay visible.
#
# Containers with such a name (the ``location`` payload, ``gps_info``) are not redacted as a whole
# by diagnostics; their entries are inspected one by one.
POSITION_WORDS = frozenset(
    {"lat", "lon", "long", "alt", "gps", "geo", "coord", "coords", "pos", "position"}
    | {"loc", "location"}
)
# Anything that looks like a credential or an identifier of the bike/battery/owner.
SECRET_SUBSTRINGS = ("serial", "token", "secret", "password", "session", "email")

# --- value patterns: the same questions asked of a VALUE, whatever its key is called.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PRIVATE_TEXT = re.compile(
    "|".join(
        (
            r"[^@\s]+@[^@\s]+\.[^@\s]+",  # e-mail address
            r"\b(?:VHC|USR)-[0-9A-Za-z]{2,}(?:-[0-9A-Za-z]{2,})*",  # vehicle / user reference id
            rf"(?<![0-9A-Za-z]){_UUID}(?![0-9A-Za-z])",  # uuid
            r"eyJ[\w-]{6,}\.[\w-]{2,}",  # JWT
            r"(?i:bearer)\s+\S{8,}",  # authorization header value
            # An opaque token: 32+ token characters containing both a letter and a digit.
            r"(?=[A-Za-z0-9_+/=.-]*\d)(?=[A-Za-z0-9_+/=.-]*[A-Za-z])[A-Za-z0-9_+/=.-]{32,}",
            # A serial-number-like string: 18 capitals/digits with at least one digit.
            r"(?<![0-9A-Za-z])(?=[0-9A-Z]*[0-9])[0-9A-Z]{18}(?![0-9A-Za-z])",
        )
    )
)
_MAX_NANODEGREES = 180_000_000_000
_EPOCH_SECONDS = (946_684_800, 4_102_444_800)


def is_private_text(value: str) -> bool:
    """True if ``value`` contains an e-mail address, an id, a token or a serial-like string."""
    return _PRIVATE_TEXT.search(value) is not None


def is_coordinate_like_number(value: object) -> bool:
    """True for a number that could be a coordinate under any key.

    * integers of 9 to 12 digits up to 180 degrees expressed in nanodegrees, except 10-digit values
      in the unix-seconds range of the years 2000-2100 (timestamps are common and harmless);
    * floats within +-180 with five or more fraction digits (degrees).
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        digits = len(str(abs(value)))
        if not 9 <= digits <= 12 or abs(value) > _MAX_NANODEGREES:
            return False
        return not (digits == 10 and _EPOCH_SECONDS[0] <= abs(value) <= _EPOCH_SECONDS[1])
    if isinstance(value, float) and abs(value) <= 180:
        text = repr(abs(value))
        return "e" not in text and len(text.partition(".")[2]) >= 5
    return False


def has_position_substring(key: str) -> bool:
    """True for keys that spell out a coordinate (``latitude``, ``longitude``, ``altitude``)."""
    lowered = key.lower()
    return any(hint in lowered for hint in POSITION_SUBSTRINGS)


def has_position_word(key: str) -> bool:
    """True when a whole word of the key is a short position hint (``lat``, ``lon``, ``gps``)."""
    return any(word.lower() in POSITION_WORDS for word in _WORDS.findall(key))


def is_position_key(key: str) -> bool:
    """True for any key that might hold position data, whatever it is called exactly."""
    return has_position_substring(key) or has_position_word(key)


def is_secret_key(key: str) -> bool:
    """True for keys that look like credentials or serial numbers."""
    lowered = key.lower()
    return any(hint in lowered for hint in SECRET_SUBSTRINGS)
