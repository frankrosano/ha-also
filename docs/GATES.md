# Gate results
Run on 2026-10-15 13:08 EDT, version 0.2.2. Command output below is verbatim (stdout and stderr combined, colour disabled); the only edit is that the machine-specific path prefix of the workspace is replaced by `<workspace>/`. No live network is used by any test, and no request to the real ALSO service was made while producing this evidence.
## `uv run ruff check`
Exit code: 0
```text
All checks passed!
```
## `uv run ruff format --check`
Exit code: 0
```text
31 files already formatted
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
collected 128 items

tests/test_availability.py .....                                         [  3%]
tests/test_config_flow.py ...................                            [ 18%]
tests/test_contract.py ......                                            [ 23%]
tests/test_diagnostics.py ...                                            [ 25%]
tests/test_init.py ................                                      [ 38%]
tests/test_lifecycle.py .......                                          [ 43%]
tests/test_no_pii.py ........                                            [ 50%]
tests/test_privacy_and_discovery.py ........                             [ 56%]
tests/test_push.py ............................................          [ 90%]
tests/test_sensor.py ............                                        [100%]

============================= 128 passed in 3.49s ==============================
```
## `python3 scripts/check_no_pii.py`
Exit code: 0
```text
PII scan: no findings.
```
## PII scan
Command: `python3 scripts/check_no_pii.py` (also run by `tests/test_no_pii.py` inside the pytest gate). The output is in the section above. The script is the same file as pyalso's, widened in 0.2.2 (letter-first serials, 9 to 12 digit nanodegree integers); `tests/test_no_pii.py` has the same runtime-generated regression cases.

Scope and limits: heuristic, files git would track, `path:line: category` only. The earlier cross-check against the real values from the private captures was NOT repeated for this change: no data from the captures was read or used, only synthetic values were added.

## Environment and evidence
* pyalso was reinstalled editable with the push extra before these runs: `uv pip install -e "<workspace>/pyalso[push]" --python <workspace>/ha-also/.venv/bin/python` (pyalso 0.2.2).
* With the integration code (`custom_components/`, `scripts/`, `hacs.json`) reverted to v0.2.1 and the new tests left in place, `uv run --no-sync pytest --continue-on-collection-errors` reported `29 failed, 99 passed` (the regression tests of `docs/REVIEW_RESPONSE.md` among them). With the 0.2.2 code: `128 passed`. This one-off comparison is not one of the gates above.
* `tests/test_contract.py` opens a local HTTP server on 127.0.0.1 (the `socket_enabled` fixture); no other host is contacted. hassfest and the HACS validation run in the `Validate` workflow and were not run locally.
