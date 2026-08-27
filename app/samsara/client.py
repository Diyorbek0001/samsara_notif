from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime, timedelta
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

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SamsaraRequestError(
                f"Samsara network request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 401:
            raise SamsaraAuthenticationError("Samsara API authentication failed")
        if response.status_code == 403:
            raise SamsaraPermissionError(
                "Samsara API token lacks media retrieval permission"
            )
        if response.status_code == 429:
            raise SamsaraRateLimitError("Samsara media retrieval quota/rate limit exceeded")
        if response.is_error:
            raise SamsaraResponseError(
                f"Unexpected Samsara HTTP status {response.status_code}"
            )
        try:
            result = response.json()
        except json.JSONDecodeError as exc:
            raise SamsaraResponseError("Samsara returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise SamsaraResponseError("Samsara response root is not an object")
        return result

    @staticmethod
    def _find_value(value: Any, names: set[str]) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in names and isinstance(item, str) and item:
                    return item
            for item in value.values():
                found = SamsaraClient._find_value(item, names)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = SamsaraClient._find_value(item, names)
                if found:
                    return found
        return None

    @staticmethod
    def _media_urls(
        value: Any,
        inherited_input: str = "snapshot",
        inherited_media_type: str = "image",
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, dict):
            camera_input = str(value.get("input") or value.get("cameraInput") or inherited_input)
            media_type = str(value.get("mediaType") or value.get("type") or inherited_media_type)
            for key in ("url", "downloadUrl", "mediaUrl"):
                url = value.get(key)
                if isinstance(url, str) and url.startswith("https://"):
                    normalized_type = "image" if media_type.lower() == "image" else "video"
                    result.append({"input": camera_input, "url": url, "mediaType": normalized_type})
            for item in value.values():
                result.extend(SamsaraClient._media_urls(item, camera_input, media_type))
        elif isinstance(value, list):
            for item in value:
                result.extend(
                    SamsaraClient._media_urls(
                        item, inherited_input, inherited_media_type
                    )
                )
        unique: dict[str, dict[str, Any]] = {}
        for item in result:
            unique.setdefault(item["url"], item)
        return list(unique.values())

    async def retrieve_camera_snapshots(
        self, vehicle_id: str, event_time: datetime
    ) -> list[dict[str, Any]]:
        return await self.retrieve_camera_media(
            vehicle_id,
            event_time,
            event_time,
            media_type="image",
        )

    async def retrieve_camera_video(
        self, vehicle_id: str, event_time: datetime
    ) -> list[dict[str, Any]]:
        # Include context immediately before and after the detected event while
        # keeping each API retrieval and Telegram upload reasonably small.
        return await self.retrieve_camera_media(
            vehicle_id,
            event_time - timedelta(seconds=10),
            event_time + timedelta(seconds=10),
            media_type="videoHighRes",
        )

    async def retrieve_camera_media(
        self,
        vehicle_id: str,
        start_time: datetime,
        end_time: datetime,
        *,
        media_type: str,
    ) -> list[dict[str, Any]]:
        start = start_time.isoformat().replace("+00:00", "Z")
        end = end_time.isoformat().replace("+00:00", "Z")
        created = await self._post(
            "/cameras/media/retrieval",
            {
                "vehicleId": vehicle_id,
                "startTime": start,
                "endTime": end,
                "mediaType": media_type,
                "inputs": ["dashcamRoadFacing", "dashcamDriverFacing"],
            },
        )
        retrieval_id = self._find_value(created, {"retrievalId", "id"})
        if not retrieval_id:
            raise SamsaraResponseError("Media retrieval response omitted retrieval ID")
        for _ in range(12):
            details = await self._get(
                "/cameras/media/retrieval", {"retrievalId": retrieval_id}
            )
            media = self._media_urls(details)
            if media:
                return media
            status = (self._find_value(details, {"status"}) or "").lower()
            if status in {"failed", "unavailable", "cancelled", "canceled"}:
                return []
            await asyncio.sleep(5)
        return []

    async def get_speeding_details(
        self, vehicle_id: str, event_time: datetime
    ) -> dict[str, Any] | None:
        start = (event_time - timedelta(hours=2)).astimezone(UTC)
        end = (event_time + timedelta(hours=2)).astimezone(UTC)
        now = datetime.now(UTC)
        if end > now:
            end = now
        params = {
            "assetIds": vehicle_id,
            "startTime": start.isoformat().replace("+00:00", "Z"),
            "endTime": end.isoformat().replace("+00:00", "Z"),
            "queryBy": "tripStartTime",
        }
        payload = await self._get("/speeding-intervals/stream", params)
        candidates: list[tuple[float, dict[str, Any]]] = []
        for trip in payload.get("data", []):
            if not isinstance(trip, dict):
                continue
            for interval in trip.get("intervals", []):
                if not isinstance(interval, dict):
                    continue
                try:
                    interval_start = datetime.fromisoformat(
                        str(interval.get("startTime", "")).replace("Z", "+00:00")
                    )
                    interval_end = datetime.fromisoformat(
                        str(interval.get("endTime", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                distance = 0.0 if interval_start <= event_time <= interval_end else min(
                    abs((event_time - interval_start).total_seconds()),
                    abs((event_time - interval_end).total_seconds()),
                )
                if distance <= 300:
                    candidates.append((distance, interval))
        if candidates:
            interval = min(candidates, key=lambda item: item[0])[1]
            maximum_kph = interval.get("maxSpeedKilometersPerHour")
            limit_kph = interval.get("postedSpeedLimitKilometersPerHour")
            maximum_mph = float(maximum_kph) * 0.621371 if maximum_kph is not None else None
            limit_mph = float(limit_kph) * 0.621371 if limit_kph is not None else None
            try:
                started = datetime.fromisoformat(str(interval["startTime"]).replace("Z", "+00:00"))
                ended = datetime.fromisoformat(str(interval["endTime"]).replace("Z", "+00:00"))
                duration = max(0, round((ended - started).total_seconds()))
            except (KeyError, ValueError):
                duration = None
            return {
                "maxSpeedMph": maximum_mph,
                "postedSpeedLimitMph": limit_mph,
                "overLimitMph": maximum_mph - limit_mph
                if maximum_mph is not None and limit_mph is not None else None,
                "durationSeconds": duration,
                "severityLevel": interval.get("severityLevel"),
                "source": "speedingIntervals",
            }

        history_start = (event_time - timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
        history_end = (event_time + timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
        history = await self._get(
            "/fleet/vehicles/stats/history",
            {
                "vehicleIds": vehicle_id,
                "types": "ecuSpeedMph",
                "startTime": history_start,
                "endTime": history_end,
            },
        )
        readings: list[float] = []
        for vehicle in history.get("data", []):
            if not isinstance(vehicle, dict):
                continue
            for reading in vehicle.get("ecuSpeedMph", []):
                if isinstance(reading, dict) and reading.get("value") is not None:
                    try:
                        readings.append(float(reading["value"]))
                    except (TypeError, ValueError):
                        pass
        if readings:
            return {"maxSpeedMph": max(readings), "source": "vehicleSpeedHistory"}
        return None

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

    async def retrieve_camera_snapshots(
        self, vehicle_id: str, event_time: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def retrieve_camera_video(
        self, vehicle_id: str, event_time: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def get_speeding_details(
        self, vehicle_id: str, event_time: datetime
    ) -> dict[str, Any] | None:
        return None
