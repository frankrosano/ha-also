"""A local fake of the ALSO REST API (plain HTTP on 127.0.0.1) for the contract tests.

The real ``pyalso.AlsoAuth`` and ``pyalso.AlsoClient`` talk to it, so token rotation, the 401
retry and the REST/push merge run pyalso's actual code. It enforces the rotating-refresh-token
contract: only the NEWEST refresh token works, a spent one is answered with HTTP 498.

Everything served here is invented. No real host is contacted.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from aiohttp import web

from .common import SHADOW, VEHICLE_ID, vehicle_dict


class FakeAlsoService:
    """Auth + vehicle endpoints with held responses and a request log."""

    def __init__(self, vehicle_ids: tuple[str, ...] = (VEHICLE_ID,)) -> None:
        self.vehicle_ids = vehicle_ids
        # The server starts out knowing the tokens the tests store in the config entry.
        self.current_access = "fake-access-token-1"
        self.current_refresh = "fake-refresh-token-1"
        self.rotations = 0
        self.refresh_bodies: list[dict[str, Any]] = []
        self.refresh_rejections = 0
        #: Authorization header value of every authenticated GET, in order.
        self.bearers: list[str] = []
        self.shadow_overrides: dict[str, dict[str, Any]] = {}
        #: How many (vehicle, endpoint) requests were fully answered, e.g. ("VHC-..", "location").
        self.answered: dict[tuple[str, str], int] = {}
        self._gates: dict[str, asyncio.Event] = {}
        self.gate_reached: dict[str, asyncio.Event] = {}
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    # ------------------------------------------------------------------ control
    def hold_shadow(self, vehicle_id: str) -> None:
        """Hold the next status-shadow response of ``vehicle_id`` until :meth:`release_shadow`."""
        self._gates[vehicle_id] = asyncio.Event()
        self.gate_reached[vehicle_id] = asyncio.Event()

    def release_shadow(self, vehicle_id: str) -> None:
        if vehicle_id in self._gates:
            self._gates[vehicle_id].set()

    async def wait_answered(self, vehicle_id: str, endpoint: str, count: int) -> None:
        """Wait until ``count`` responses of ``endpoint`` for ``vehicle_id`` were sent."""
        while self.answered.get((vehicle_id, endpoint), 0) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)  # a polled counter in a test helper

    # ----------------------------------------------------------------- handlers
    async def _refresh(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.refresh_bodies.append(body)
        if body.get("refresh_token") != self.current_refresh:
            self.refresh_rejections += 1
            return web.json_response({"message": "session expired"}, status=498)
        self.rotations += 1
        self.current_access = f"rotated-access-{self.rotations}"
        self.current_refresh = f"rotated-refresh-{self.rotations}"
        return web.json_response(
            {
                "access_token": self.current_access,
                "refresh_token": self.current_refresh,
                "id_token": f"rotated-id-{self.rotations}",
                "expires_in": 86400,
            }
        )

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        self.bearers.append(header)
        return header == f"Bearer {self.current_access}"

    async def _vehicles(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        items = [
            vehicle_dict(vid, f"CONTRACTSERIAL{n:04d}") for n, vid in enumerate(self.vehicle_ids)
        ]
        return web.json_response({"items": items, "total": len(items)})

    async def _shadow(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        vid = request.match_info["vid"]
        shadow = copy.deepcopy(SHADOW) | self.shadow_overrides.get(vid, {})
        gate = self._gates.get(vid)
        if gate is not None:
            self.gate_reached[vid].set()
            await gate.wait()
            self._gates.pop(vid, None)
        return web.json_response(shadow)

    async def _location(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        self.answered[(request.match_info["vid"], "location")] = (
            self.answered.get((request.match_info["vid"], "location"), 0) + 1
        )
        return web.json_response(
            {
                "timestamp": "2023-11-14T22:13:20Z",
                "latitude": SHADOW["con_aLatitude"],
                "longitude": SHADOW["con_aLongitude"],
                "altitude": 1000,
                "gps_info": {"accuracy": 5, "gps_time": 1_700_000_000, "satellite_count": 9},
            }
        )

    async def _connectivity(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        key = (request.match_info["vid"], "connectivity")
        self.answered[key] = self.answered.get(key, 0) + 1
        return web.json_response({"connected": True, "disconnect_reason": "TEST_REASON"})

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/api/v1/auth/cognito/refresh", self._refresh)
        app.router.add_get("/api/v2/vehicles/me", self._vehicles)
        app.router.add_get("/api/v2/vehicles/{vid}/shadow/status", self._shadow)
        app.router.add_get("/api/v2/vehicles/{vid}/location", self._location)
        app.router.add_get("/api/v2/vehicles/{vid}/connectivity", self._connectivity)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and hasattr(server, "sockets")
        port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for gate in self._gates.values():
            gate.set()
        if self._runner is not None:
            await self._runner.cleanup()
