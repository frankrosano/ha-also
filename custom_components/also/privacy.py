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
