"""The brand images Home Assistant looks for (2026.3 and newer) exist and have valid sizes."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "also" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 100_000

X2 = "@2x"  # file name suffix of the high-resolution images
ICONS = {"icon.png": 256, f"icon{X2}.png": 512}
# name: (smallest and largest allowed length of the shorter side)
LOGOS = {
    "logo.png": (128, 256),
    f"logo{X2}.png": (256, 512),
    "dark_logo.png": (128, 256),
    f"dark_logo{X2}.png": (256, 512),
}


def _png_size(path: Path) -> tuple[int, int]:
    """Return (width, height) from the IHDR chunk, after checking signature and trailer."""
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE), f"{path.name} is not a PNG"
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR"
    assert length == 13
    width, height = struct.unpack(">II", data[16:24])
    assert data.endswith(b"IEND\xaeB`\x82"), f"{path.name} is truncated"
    return width, height


@pytest.mark.parametrize(("name", "size"), ICONS.items())
def test_icon_is_square_with_expected_size(name: str, size: int) -> None:
    assert _png_size(BRAND / name) == (size, size)


@pytest.mark.parametrize(("name", "limits"), LOGOS.items())
def test_logo_shorter_side_in_range(name: str, limits: tuple[int, int]) -> None:
    width, height = _png_size(BRAND / name)
    low, high = limits
    assert low <= min(width, height) <= high


@pytest.mark.parametrize("prefix", ["logo", "dark_logo"])
def test_logo_2x_has_double_the_size(prefix: str) -> None:
    width, height = _png_size(BRAND / f"{prefix}.png")
    width2, height2 = _png_size(BRAND / f"{prefix}{X2}.png")
    assert height2 == 2 * height
    assert abs(width2 - 2 * width) <= 2


@pytest.mark.parametrize("name", [*ICONS, *LOGOS])
def test_file_is_small(name: str) -> None:
    assert 0 < (BRAND / name).stat().st_size < MAX_BYTES


def test_no_unexpected_files() -> None:
    assert {p.name for p in BRAND.iterdir()} == {*ICONS, *LOGOS}
