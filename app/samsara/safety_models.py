from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SafetyStreamPage:
    data: list[dict[str, Any]]
    end_cursor: str
    has_next_page: bool


@dataclass(frozen=True)
class NormalizedSafetyEvent:
    samsara_event_id: str
    event_timestamp: datetime
    vehicle_id: str | None
    vehicle_name: str | None
    vehicle_vin: str | None
    driver_id: str | None
    driver_name: str | None
    severity: str
    primary_behavior: str
    behaviors: list[str]
    contexts: list[str]
    event_state: str | None
    latitude: float | None
    longitude: float | None
    location_name: str | None
    media: list[dict[str, Any]]
    raw: dict[str, Any]

