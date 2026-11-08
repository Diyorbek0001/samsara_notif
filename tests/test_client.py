from datetime import UTC, datetime

import httpx
import pytest

from app.samsara.client import SamsaraClient
from app.samsara.exceptions import SamsaraAuthenticationError, SamsaraRateLimitError


@pytest.mark.asyncio
async def test_feed_request_and_cursor():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["types"] == "faultCodes"
        assert request.url.params["after"] == "old"
        return httpx.Response(200, json={"data": [], "pagination": {"endCursor": "new", "hasNextPage": False}})

    client = SamsaraClient("secret", "https://api.samsara.com", transport=httpx.MockTransport(handler))
    page = await client.get_fault_code_feed("old")
    await client.close()
    assert page.end_cursor == "new"


@pytest.mark.asyncio
async def test_authentication_error():
    client = SamsaraClient(
        "secret",
        "https://api.samsara.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    with pytest.raises(SamsaraAuthenticationError):
        await client.get_fault_code_feed()
    await client.close()


@pytest.mark.asyncio
async def test_429_honors_retry_after_at_limit():
    client = SamsaraClient(
        "secret",
        "https://api.samsara.com",
        max_retries=1,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "7"})
        ),
    )
    with pytest.raises(SamsaraRateLimitError) as error:
        await client.get_fault_code_feed()
    await client.close()
    assert error.value.retry_after == 7


def test_media_urls_extracts_and_deduplicates_snapshots():
    payload = {
        "data": {
            "media": [
                {
                    "input": "dashcamDriverFacing",
                    "url": "https://example.invalid/snapshot.jpg",
                },
                {"url": "https://example.invalid/snapshot.jpg"},
            ]
        }
    }
    assert SamsaraClient._media_urls(payload) == [
        {
            "input": "dashcamDriverFacing",
            "url": "https://example.invalid/snapshot.jpg",
            "mediaType": "image",
        }
    ]


def test_media_urls_preserves_video_media_type():
    payload = {
        "data": {
            "mediaType": "videoHighRes",
            "media": [
                {
                    "input": "dashcamRoadFacing",
                    "url": "https://example.invalid/video.mp4",
                }
            ],
        }
    }
    assert SamsaraClient._media_urls(payload) == [
        {
            "input": "dashcamRoadFacing",
            "url": "https://example.invalid/video.mp4",
            "mediaType": "video",
        }
    ]


@pytest.mark.asyncio
async def test_speeding_video_requests_twenty_seconds_around_event():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"retrievalId": "retrieval-1"}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "complete",
                    "mediaType": "videoHighRes",
                    "media": [{"url": "https://example.invalid/video.mp4"}],
                }
            },
        )

    client = SamsaraClient(
        "secret", "https://api.samsara.com", transport=httpx.MockTransport(handler)
    )
    media = await client.retrieve_camera_video(
        "vehicle-1", datetime(2026, 8, 3, 18, 59, 30, tzinfo=UTC)
    )
    await client.close()
    body = __import__("json").loads(requests[0].content)
    assert body["startTime"] == "2026-08-03T18:59:20Z"
    assert body["endTime"] == "2026-08-03T18:59:40Z"
    assert body["mediaType"] == "videoHighRes"
    assert media[0]["mediaType"] == "video"


@pytest.mark.asyncio
async def test_speeding_details_matches_event_interval_and_converts_to_mph():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speeding-intervals/stream"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "intervals": [
                            {
                                "startTime": "2026-08-03T18:59:00Z",
                                "endTime": "2026-08-03T19:00:00Z",
                                "maxSpeedKilometersPerHour": 128.747,
                                "postedSpeedLimitKilometersPerHour": 104.607,
                                "severityLevel": "severe",
                            }
                        ]
                    }
                ]
            },
        )

    client = SamsaraClient(
        "secret", "https://api.samsara.com", transport=httpx.MockTransport(handler)
    )
    details = await client.get_speeding_details(
        "vehicle-1", datetime(2026, 8, 3, 18, 59, 30, tzinfo=UTC)
    )
    await client.close()
    assert details is not None
    assert round(details["maxSpeedMph"]) == 80
    assert round(details["postedSpeedLimitMph"]) == 65
    assert round(details["overLimitMph"]) == 15
    assert details["durationSeconds"] == 60
