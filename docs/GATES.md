# Gate results

Run on 2026-10-14 17:01 EDT, version 0.2.1. Command output below is verbatim (stdout and stderr combined, colour disabled); the only edit is that the machine-specific path prefix of the workspace is replaced by `<workspace>/`. No live network is used by any test.

## `uv run ruff check`
Exit code: 0
```text
[1;32mAll checks passed![0m
```

## `uv run ruff format --check`
Exit code: 0
```text
24 files already formatted
```

## `uv run pytest`
Exit code: 0
```text
============================= test session starts ==============================
platform darwin -- Python 3.13.13, pytest-9.0.0, pluggy-1.6.0
rootdir: <workspace>/ha-also
configfile: pyproject.toml
testpaths: tests
plugins: respx-0.22.0, pytest_freezer-0.4.9, unordered-0.7.0, homeassistant-custom-component-0.13.316, sugar-1.0.0, socket-0.7.0, xdist-3.8.0, timeout-2.4.0, github-actions-annotate-failures-0.3.0, asyncio-1.3.0, aiohttp-1.1.0, anyio-4.15.1, picked-0.5.1, requests-mock-1.12.1, cov-7.0.0, syrupy-5.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 94 items

tests/test_config_flow.py ...............                                [ 15%]
tests/test_diagnostics.py ...                                            [ 19%]
tests/test_init.py ................                                      [ 36%]
tests/test_no_pii.py .....                                               [ 41%]
tests/test_push.py ...........................................           [ 87%]
tests/test_sensor.py ............                                        [100%]

============================== 94 passed in 1.24s ==============================
```

## PII scan
Command: `python3 scripts/check_no_pii.py` (also run by `tests/test_no_pii.py` inside the pytest gate).
Exit code: 0
```text
PII scan: no findings.
```

**Cross-check against the real values.** Outside the repository (so no real value is written into any file here), the private capture logs `probe_auth.log`, `probe_vehicle*.log` and `shadow_status.json` were read to extract the real e-mail, names, user ids and uuids, user and vehicle reference ids, bike serial, coordinates (nanodegree integers and decimal degrees), altitude and timestamps, and every file of this repository (README, `docs/`, `custom_components/`, `tests/`, `scripts/`, `.agents/`) was searched for each of them. Result: no occurrence of any of them. The only hit left is the machine-specific home-directory path inside the git-ignored `.agents/` planning artefacts (developer-local, never committed). The battery serial is not in those three logs, so it could only be covered by the pattern scan above (serial-like strings other than the all-zero fake are flagged); this repository uses `TESTSERIAL...` serials.

Scope and limits: the scanner looks at files git would track (committed, or untracked and not ignored). It is a heuristic and reports `path:line: category` only, never the matched text.
