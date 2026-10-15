#!/usr/bin/env python3
"""Fail when files that would be committed contain obvious personal data.

    python3 scripts/check_no_pii.py [ROOT]

Looks (in files git would track: committed or untracked-but-not-ignored) for:

* e-mail addresses outside example.com / example.org / example.net;
* ALSO vehicle (``VHC-xxxx-xxxx-xxxx``) and user (``USR-xxxx-xxxx-xxxx``) reference ids other than
  the fake ``AAAA-BBBB-<CCCC|DDDD|EEEE>`` ones (users: ``CCCC`` and ``DDDD``);
* 18-character serial-number-like strings starting with a digit (other than all zeros);
* UUIDs that are not obviously fake (all zeros, or one repeated digit);
* coordinates: 11-digit integers (nanodegrees of 10 degrees or more) and decimals with six or more
  fraction digits and an absolute value of 10 or more. Fake data must stay near 0,0;
* JWT-looking tokens and long bearer tokens;
* machine-specific home-directory paths (a line containing ``developer-local`` is exempt).

These are heuristics: they catch the obvious, not everything. Matched text is NEVER printed (the
report would leak what it is meant to prevent): only ``path:line: category``. Exit status 1 if
anything was found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ALLOWED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
ALLOWED_VEHICLE_IDS = frozenset({"VHC-AAAA-BBBB-CCCC", "VHC-AAAA-BBBB-DDDD", "VHC-AAAA-BBBB-EEEE"})
ALLOWED_USER_IDS = frozenset({"USR-AAAA-BBBB-CCCC", "USR-AAAA-BBBB-DDDD"})
ALLOWED_SERIALS = frozenset({"000000000000000000"})
EXEMPT_MARKER = "developer-local"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_VEHICLE = re.compile(r"\bVHC-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b")
_USER = re.compile(r"\bUSR-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}\b")
_SERIAL = re.compile(r"(?<![0-9A-Za-z])[0-9][0-9A-Z]{17}(?![0-9A-Za-z])")
_UUID = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9A-Za-z])"
)
_FAKE_UUIDS = (
    re.compile(r"0{8}-0{4}-[0-9a-f]000-[0-9a-f]000-0{11}[0-9a-f]", re.IGNORECASE),
    re.compile(r"([0-9a-f])\1{7}-\1{4}-[0-9a-f]\1{3}-[0-9a-f]\1{3}-\1{12}", re.IGNORECASE),
)
_NANO = re.compile(r"(?<![0-9A-Za-z_.])-?[0-9]{11}(?![0-9A-Za-z_.])")
# ``(?<![0-9]:)`` skips clock times such as ``16:59:46.890053`` in log output.
_DECIMAL = re.compile(r"(?<![0-9A-Za-z_.])(?<![0-9]:)(-?[0-9]{1,3})\.[0-9]{6,}(?![0-9A-Za-z_.])")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{30,}")
_HOME_PATH = re.compile("(?:/" + "Users" + "|/" + "home" + r")/[^/\s'\"`]+/|[A-Za-z]:\\\\Users\\\\")

_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)
_SKIP_NAMES = frozenset({"uv.lock"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One suspicious line. Deliberately carries no matched text."""

    path: str
    line: int
    category: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.category}"


def scan_text(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, category)`` for every suspicious line of ``text``."""
    for number, line in enumerate(text.splitlines(), start=1):
        exempt = EXEMPT_MARKER in line
        for match in _EMAIL.finditer(line):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                yield number, "e-mail address"
        for match in _VEHICLE.finditer(line):
            if match.group(0) not in ALLOWED_VEHICLE_IDS:
                yield number, "vehicle reference id"
        for match in _USER.finditer(line):
            if match.group(0) not in ALLOWED_USER_IDS:
                yield number, "user reference id"
        for match in _SERIAL.finditer(line):
            if match.group(0) not in ALLOWED_SERIALS:
                yield number, "serial-number-like string"
        for match in _UUID.finditer(line):
            if not any(fake.fullmatch(match.group(0)) for fake in _FAKE_UUIDS):
                yield number, "uuid"
        for _ in _NANO.finditer(line):
            yield number, "coordinate-like integer (nanodegrees of 10 degrees or more)"
        for match in _DECIMAL.finditer(line):
            if abs(int(match.group(1))) >= 10:
                yield number, "coordinate-like decimal (10 degrees or more)"
        if _JWT.search(line) or _BEARER.search(line):
            yield number, "token"
        if not exempt and _HOME_PATH.search(line):
            yield number, "machine-specific path"


def candidate_files(root: Path) -> list[Path]:
    """Files git would track (committed, or untracked and not ignored); a tree walk without git."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", "replace")
        names = [n for n in out.split("\0") if n]
        return [root / n for n in names if (root / n).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in root.rglob("*")
            if p.is_file() and not _SKIP_DIRS.intersection(p.relative_to(root).parts)
        ]


def scan(root: Path, files: Iterable[Path] | None = None) -> list[Finding]:
    """Scan ``root`` (or the given files) and return the findings."""
    findings: list[Finding] = []
    for path in files if files is not None else candidate_files(root):
        if path.name in _SKIP_NAMES or _SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        rel = str(path.relative_to(root))
        findings.extend(Finding(rel, number, category) for number, category in scan_text(text))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    findings = scan(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} suspicious line(s). Replace real data with placeholders.")
        return 1
    print("PII scan: no findings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
