from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FeedPage:
    data: list[dict[str, Any]]
    end_cursor: str
    has_next_page: bool


@dataclass(frozen=True)
class NormalizedFault:
    vehicle_id: str
    vehicle_name: str | None
    protocol: str
    spn: str | None
    fmi: str | None
    obd_code: str | None
    manufacturer_code: str | None
    source: str | None
    normalized_code: str
    definition_identity: str
    fingerprint: str
    description: str | None
    occurrence_count: int | None
    timestamp: datetime | None
    explicit_active: bool | None
    severity: str
    raw: dict[str, Any]

