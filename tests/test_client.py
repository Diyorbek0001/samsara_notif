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
