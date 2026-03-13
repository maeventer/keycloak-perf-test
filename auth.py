"""Shared Keycloak admin token management with asyncio-safe refresh."""

import asyncio
import base64
import json
import time
import httpx
import config


class TokenManager:
    """Obtains and refreshes a Keycloak admin access token.

    Safe for concurrent async use: an asyncio.Lock prevents multiple
    coroutines from refreshing simultaneously.
    """

    def __init__(self):
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._exp: float = 0.0
        self._refresh_exp: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing if needed."""
        if time.time() < self._exp - 30:
            return self._access_token
        async with self._lock:
            # Re-check after acquiring lock (another coroutine may have refreshed)
            if time.time() < self._exp - 30:
                return self._access_token
            if self._refresh_token and time.time() < self._refresh_exp - 30:
                await self._do_refresh(client)
            else:
                await self._do_login(client)
        return self._access_token

    async def _do_login(self, client: httpx.AsyncClient):
        url = f"{config.KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
        resp = await client.post(url, data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": config.ADMIN_USER,
            "password": config.ADMIN_PASSWORD,
        })
        resp.raise_for_status()
        self._store(resp.json())

    async def _do_refresh(self, client: httpx.AsyncClient):
        url = f"{config.KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
        resp = await client.post(url, data={
            "grant_type": "refresh_token",
            "client_id": "admin-cli",
            "refresh_token": self._refresh_token,
        })
        if resp.status_code != 200:
            # Refresh token expired or server error; fall back to full login
            await self._do_login(client)
            return
        self._store(resp.json())

    def _store(self, data: dict):
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", "")
        self._exp = _jwt_exp(self._access_token)
        self._refresh_exp = _jwt_exp(self._refresh_token) if self._refresh_token else 0.0


def _jwt_exp(token: str) -> float:
    """Decode the exp claim from a JWT without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding so base64 decodes correctly
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload["exp"])
    except Exception:
        # If decoding fails, treat as already expired
        return 0.0
