from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

import httpx

from .exceptions import (
    SamsaraAuthenticationError,
    SamsaraCursorError,
    SamsaraNotFoundError,
    SamsaraPermissionError,
    SamsaraRateLimitError,
    SamsaraRequestError,
    SamsaraResponseError,
)
from .models import FeedPage
from .safety_models import SafetyStreamPage

logger = logging.getLogger(__name__)


class SamsaraClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        timeout: float = 30,
        max_retries: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 == self.max_retries:
                    raise SamsaraRequestError(f"Samsara network request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(min(2**attempt + random.random(), 300))
                continue
            if response.status_code == 401:
                raise SamsaraAuthenticationError("Samsara API authentication failed")
            if response.status_code == 403:
                raise SamsaraPermissionError("Samsara API token lacks the required scope or tag access")
            if response.status_code == 404:
                raise SamsaraNotFoundError("Samsara endpoint or resource was not found")
            if response.status_code == 400 and params.get("after"):
                raise SamsaraCursorError("Samsara rejected the saved feed cursor")
            if response.status_code == 429:
                retry = response.headers.get("Retry-After")
                retry_after = float(retry) if retry and retry.replace(".", "", 1).isdigit() else None
                if attempt + 1 == self.max_retries:
                    raise SamsaraRateLimitError("Samsara rate limit exceeded", retry_after)
                await asyncio.sleep(retry_after or min(2**attempt + random.random(), 300))
                continue
            if response.status_code >= 500:
                if attempt + 1 == self.max_retries:
                    raise SamsaraRequestError(f"Samsara server error HTTP {response.status_code}")
                await asyncio.sleep(min(2**attempt + random.random(), 300))
                continue
            if response.is_error:
                raise SamsaraResponseError(f"Unexpected Samsara HTTP status {response.status_code}")
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise SamsaraResponseError("Samsara returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise SamsaraResponseError("Samsara response root is not an object")
            return payload
        raise SamsaraRequestError("Samsara request exhausted retries")

    async def get_fault_code_feed(self, after: str | None = None) -> FeedPage:
        params: dict[str, Any] = {"types": "faultCodes"}
        if after:
            params["after"] = after
        payload = await self._get("/fleet/vehicles/stats/feed", params)
        data = payload.get("data")
        pagination = payload.get("pagination")
        if not isinstance(data, list) or not isinstance(pagination, dict):
            raise SamsaraResponseError("Samsara feed omitted data or pagination")
        cursor = pagination.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise SamsaraResponseError("Samsara feed omitted a valid endCursor")
        return FeedPage(data=data, end_cursor=cursor, has_next_page=bool(pagination.get("hasNextPage")))

    async def get_vehicles(self) -> list[dict[str, Any]]:
        after: str | None = None
        result: list[dict[str, Any]] = []
        while True:
            params = {"after": after} if after else {}
            payload = await self._get("/fleet/vehicles", params)
            data, pagination = payload.get("data"), payload.get("pagination", {})
            if not isinstance(data, list):
                raise SamsaraResponseError("Vehicle response omitted data")
            result.extend(item for item in data if isinstance(item, dict))
            if not pagination.get("hasNextPage"):
                return result
            after = pagination.get("endCursor")
            if not after:
                raise SamsaraResponseError("Vehicle pagination omitted endCursor")

    async def get_safety_event_stream(
        self,
        *,
        start_time: str,
        after: str | None = None,
        end_time: str | None = None,
        query_by_time_field: str = "updatedAtTime",
    ) -> SafetyStreamPage:
        params: dict[str, Any] = {
            "startTime": start_time,
            "queryByTimeField": query_by_time_field,
            "includeAsset": "true",
            "includeDriver": "true",
        }
        if after:
            params["after"] = after
        if end_time:
            params["endTime"] = end_time
        payload = await self._get("/safety-events/stream", params)
        data, pagination = payload.get("data"), payload.get("pagination")
        if not isinstance(data, list) or not isinstance(pagination, dict):
            raise SamsaraResponseError("Safety stream omitted data or pagination")
        cursor = pagination.get("endCursor")
        if not isinstance(cursor, str):
            raise SamsaraResponseError("Safety stream omitted a valid endCursor")
        if not cursor and pagination.get("hasNextPage"):
            raise SamsaraResponseError("Safety stream omitted a valid endCursor")
        return SafetyStreamPage(
            [item for item in data if isinstance(item, dict)],
            cursor,
            bool(pagination.get("hasNextPage")),
        )


class MockSamsaraClient:
    def __init__(self, fixture: Path):
        self.fixture = fixture

    async def close(self) -> None:
        return None

    async def get_fault_code_feed(self, after: str | None = None) -> FeedPage:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        pagination = payload["pagination"]
        return FeedPage(payload["data"], pagination["endCursor"], pagination["hasNextPage"])

    async def get_vehicles(self) -> list[dict[str, Any]]:
        return []

    async def get_safety_event_stream(
        self,
        *,
        start_time: str,
        after: str | None = None,
        end_time: str | None = None,
        query_by_time_field: str = "updatedAtTime",
    ) -> SafetyStreamPage:
        return SafetyStreamPage([], "mock-safety-cursor", False)
