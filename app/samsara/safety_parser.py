from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .safety_models import NormalizedSafetyEvent


CRITICAL = {"crash", "harshimpact", "rolloverprotection"}
HIGH = {"drowsy", "severespeeding", "forwardcollisionwarning", "nearcollison"}
MODERATE = {
    "mobileusage",
    "genericdistraction",
    "braking",
    "acceleration",
    "harshturn",
    "rollingstop",
    "generictailgating",
    "followingdistance",
    "noseatbelt",
}
LOW = {"obstructedcamera"}


def severity_for(behaviors: list[str]) -> str:
    normalized = {label.replace(" ", "").lower() for label in behaviors}
    if normalized & CRITICAL:
        return "critical"
    if normalized & HIGH:
        return "high"
    if normalized & MODERATE:
        return "moderate"
    if normalized & LOW:
        return "low"
    return "unknown"


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        label = item.get("label") if isinstance(item, dict) else item
        if label is not None and str(label).strip():
            result.append(str(label).strip())
    return list(dict.fromkeys(result))


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _event_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC)
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OSError):
        return None


def _safe_media(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if url and urlparse(str(url)).scheme != "https":
            url = None
        result.append({"input": str(item.get("input") or "unknown"), "url": str(url) if url else None})
    return result


def _address(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    street = " ".join(
        str(value.get(key) or "").strip() for key in ("streetNumber", "street")
    ).strip()
    city = str(value.get("city") or "").strip()
    state = str(value.get("state") or "").strip()
    postal_code = str(value.get("postalCode") or "").strip()
    region = " ".join(part for part in (state, postal_code) if part)
    locality = ", ".join(part for part in (city, region) if part)
    country = str(value.get("country") or "").strip()
    parts = [part for part in (street, locality, country) if part]
    return ", ".join(parts) or None


def parse_safety_event(raw: dict[str, Any]) -> NormalizedSafetyEvent | None:
    event_id = raw.get("id")
    start_ms = raw.get("startMs")
    if not event_id or start_ms is None:
        return None
    event_time = _event_time(start_ms)
    if event_time is None:
        return None
    asset = raw.get("asset") if isinstance(raw.get("asset"), dict) else {}
    driver = raw.get("driver") if isinstance(raw.get("driver"), dict) else {}
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    behaviors = _labels(raw.get("behaviorLabels"))
    contexts = _labels(raw.get("contextLabels"))
    primary = behaviors[0] if behaviors else "Unknown Samsara Safety Event"
    return NormalizedSafetyEvent(
        samsara_event_id=str(event_id),
        event_timestamp=event_time,
        vehicle_id=str(asset["id"]) if asset.get("id") is not None else None,
        vehicle_name=str(asset["name"]) if asset.get("name") else None,
        vehicle_vin=str(asset["vin"]) if asset.get("vin") else None,
        driver_id=str(driver["id"]) if driver.get("id") is not None else None,
        driver_name=str(driver["name"]) if driver.get("name") else None,
        severity=severity_for(behaviors),
        primary_behavior=primary,
        behaviors=behaviors,
        contexts=contexts,
        event_state=str(raw["eventState"]) if raw.get("eventState") else None,
        latitude=_float(location.get("latitude")),
        longitude=_float(location.get("longitude")),
        location_name=_address(location.get("address")),
        media=_safe_media(raw.get("media")),
        raw=raw,
    )
