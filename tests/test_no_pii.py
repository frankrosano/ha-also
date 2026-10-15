"""The PII scanner works, and the repository passes it (no real personal data in tracked files)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "check_no_pii.py"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/ is not part of an sdist")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_no_pii", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through it
    spec.loader.exec_module(module)
    return module


def _categories(text: str) -> set[str]:
    return {category for _, category in _load().scan_text(text)}


def test_repository_has_no_personal_data() -> None:
    findings = _load().scan(ROOT)
    assert [str(f) for f in findings] == []  # path:line: category (never the matched text)


def test_fake_placeholders_pass() -> None:
    fakes = "\n".join(
        [
            "user@example.com  VHC-AAAA-BBBB-CCCC  USR-AAAA-BBBB-CCCC",
            "serial 000000000000000000 and FAKE00000000000001",
            "uuid 00000000-0000-4000-8000-000000000001 11111111-1111-4111-8111-111111111111",
            "lat 1_234_567_890 nano, 0.012345 deg, version 0.17.0, ruff==0.17.0, t=1700000000",
            "I1014 16:59:46.890053 24213120 log line with a clock time",
        ]
    )
    assert _categories(fakes) == set()


def test_obvious_personal_data_is_caught() -> None:
    # Built at runtime so this file itself stays clean.
    email = "someone" + "@" + "gmail.com"
    vehicle = "VHC-" + "0ABC-D12E-3FG4"
    user = "USR-" + "0ABC-D12E-3FG4"
    serial = "9Q" + "XXXXXXXX" + "12345678"
    uuid = "1a2b3c4d-" + "5e6f-4a1b-8c2d-" + "3e4f5a6b7c8d"
    nano = "-" + "1234" + "5678901"
    decimal = "12." + "345678"
    token = "eyJ" + "hbGciOiJSUzI1NiJ9" + ".payload123"
    home = "/" + "Users" + "/someone/Projects/x"
    assert _categories(email) == {"e-mail address"}
    assert _categories(vehicle) == {"vehicle reference id"}
    assert _categories(user) == {"user reference id"}
    assert _categories(serial) == {"serial-number-like string"}
    assert _categories(uuid) == {"uuid"}
    assert len(_categories(nano)) == 1 and len(_categories(decimal)) == 1
    assert _categories(token) == {"token"}
    assert _categories(home) == {"machine-specific path"}
    assert _categories(home + "  # developer-local note") == set()


def test_report_never_contains_the_matched_text(tmp_path: Path) -> None:
    module = _load()
    secret = "someone" + "@" + "gmail.com"
    (tmp_path / "notes.md").write_text(f"contact {secret}\n", encoding="utf-8")
    findings = module.scan(tmp_path, files=[tmp_path / "notes.md"])
    assert [str(f) for f in findings] == ["notes.md:1: e-mail address"]
    assert secret not in str(findings[0])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_main_returns_nonzero_on_findings(tmp_path: Path) -> None:
    module = _load()
    (tmp_path / "a.txt").write_text("fine\n", encoding="utf-8")
    assert module.main([str(tmp_path)]) == 0
    (tmp_path / "b.txt").write_text("reach me at " + "x" + "@" + "gmail.com\n", encoding="utf-8")
    assert module.main([str(tmp_path)]) == 1
