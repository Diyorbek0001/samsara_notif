from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Driver,
    NotificationOutbox,
    SafetyEvent,
    SafetyEventUpdate,
    SafetySequence,
    Vehicle,
)
from app.samsara.safety_models import NormalizedSafetyEvent


def utcnow() -> datetime:
    return datetime.now(UTC)


def redacted_raw(event: NormalizedSafetyEvent) -> dict:
    raw = dict(event.raw)
    media = []
    for item in event.media:
        media.append(
            {
                "input": item.get("input"),
                "urlHash": hashlib.sha256((item.get("url") or "").encode()).hexdigest()
                if item.get("url")
                else None,
            }
        )
    raw["media"] = media
    return raw


def media_signature(media: list[dict]) -> str | None:
    """Stable signature for available camera inputs, excluding expiring URLs."""
    inputs = sorted(
        str(item.get("input") or "unknown")
        for item in media
        if item.get("url")
    )
    if not inputs:
        return None
    return hashlib.sha256("\n".join(inputs).encode()).hexdigest()[:20]


async def next_alert_id(session: AsyncSession, timezone: str) -> str:
    local_date = utcnow().astimezone(ZoneInfo(timezone)).date().isoformat()
    sequence = await session.get(SafetySequence, local_date)
    if sequence is None:
        sequence = SafetySequence(local_date=local_date, last_value=1)
        session.add(sequence)
        value = 1
    else:
        sequence.last_value += 1
        value = sequence.last_value
    await session.flush()
    return f"SAFE-{local_date.replace('-', '')}-{value:04d}"


async def _vehicle(session: AsyncSession, event: NormalizedSafetyEvent) -> Vehicle | None:
    if not event.vehicle_id:
        return None
    vehicle = await session.scalar(
        select(Vehicle).where(Vehicle.samsara_vehicle_id == event.vehicle_id)
    )
    now = utcnow()
    if vehicle is None:
        vehicle = Vehicle(
            samsara_vehicle_id=event.vehicle_id,
            vehicle_name=event.vehicle_name,
            vin=event.vehicle_vin,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(vehicle)
        await session.flush()
    else:
        vehicle.vehicle_name = event.vehicle_name or vehicle.vehicle_name
        vehicle.vin = event.vehicle_vin or vehicle.vin
        vehicle.updated_at = now
    return vehicle


async def _driver(session: AsyncSession, event: NormalizedSafetyEvent) -> Driver | None:
    if not event.driver_id:
        return None
    driver = await session.scalar(
        select(Driver).where(Driver.samsara_driver_id == event.driver_id)
    )
    now = utcnow()
    if driver is None:
        driver = Driver(
            samsara_driver_id=event.driver_id,
            driver_name=event.driver_name,
            external_ids_json={},
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(driver)
        await session.flush()
    elif event.driver_name:
        driver.driver_name, driver.updated_at = event.driver_name, now
    return driver


async def process_safety_event(
    session: AsyncSession,
    event: NormalizedSafetyEvent,
    *,
    initialization: bool,
    send_initial: bool,
    timezone: str,
    integration_id: int | None = None,
    notification_delay_seconds: int = 90,
) -> tuple[SafetyEvent, str]:
    existing = await session.scalar(
        select(SafetyEvent).where(SafetyEvent.samsara_event_id == event.samsara_event_id)
    )
    now = utcnow()
    safe_raw = redacted_raw(event)
    vehicle = await _vehicle(session, event)
    driver = await _driver(session, event)
    if existing:
        previous_speed_details = (
            existing.raw_event_json.get("speedingDetails")
            if isinstance(existing.raw_event_json, dict)
            else None
        )
        if (
            "speedingDetails" not in safe_raw
            and isinstance(existing.raw_event_json, dict)
            and existing.raw_event_json.get("speedingDetails")
        ):
            safe_raw["speedingDetails"] = existing.raw_event_json["speedingDetails"]
        speed_details_changed = (
            safe_raw.get("speedingDetails") is not None
            and safe_raw.get("speedingDetails") != previous_speed_details
        )
        previous_media_signature = media_signature(existing.media_json)
        new_media_signature = media_signature(event.media)
        changed = (
            existing.event_state != event.event_state
            or existing.behavior_labels_json != event.behaviors
            or existing.media_json != event.media
            or speed_details_changed
        )
        existing.vehicle_id = vehicle.id if vehicle else existing.vehicle_id
        existing.driver_id = driver.id if driver else existing.driver_id
        existing.severity = event.severity
        existing.primary_behavior = event.primary_behavior
        existing.behavior_labels_json = event.behaviors
        existing.context_labels_json = event.contexts
        existing.event_state = event.event_state
        existing.latitude, existing.longitude = event.latitude, event.longitude
        existing.location_name = event.location_name
        existing.media_json = event.media
        existing.raw_event_json = safe_raw
        existing.updated_at = now
        if changed:
            session.add(
                SafetyEventUpdate(
                    safety_event_id=existing.id,
                    event_type="updated",
                    received_at=now,
                    raw_update_json=safe_raw,
                    created_at=now,
                )
            )
        if new_media_signature and new_media_signature != previous_media_signature:
            unsent_initial = await session.scalar(
                select(NotificationOutbox.id).where(
                    NotificationOutbox.safety_event_id == existing.id,
                    NotificationOutbox.notification_type == "new",
                    NotificationOutbox.status.in_(("pending", "failed", "processing")),
                )
            )
            if unsent_initial is None:
                session.add(
                    NotificationOutbox(
                        safety_event_id=existing.id,
                        category="safety",
                        notification_type="media_update",
                        idempotency_key=(
                            f"safety:media:{event.samsara_event_id}:{new_media_signature}"
                        ),
                        payload_json={
                            "safety_event_id": existing.id,
                            "alert_id": existing.alert_id,
                            "integration_id": integration_id,
                        },
                        status="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
        if speed_details_changed:
            unsent_initial = await session.scalar(
                select(NotificationOutbox.id).where(
                    NotificationOutbox.safety_event_id == existing.id,
                    NotificationOutbox.notification_type == "new",
                    NotificationOutbox.status.in_(("pending", "failed", "processing")),
                )
            )
            if unsent_initial is None:
                speed_signature = hashlib.sha256(
                    str(safe_raw["speedingDetails"]).encode()
                ).hexdigest()[:20]
                session.add(
                    NotificationOutbox(
                        safety_event_id=existing.id,
                        category="safety",
                        notification_type="speed_update",
                        idempotency_key=(
                            f"safety:speed:{event.samsara_event_id}:{speed_signature}"
                        ),
                        payload_json={
                            "safety_event_id": existing.id,
                            "alert_id": existing.alert_id,
                            "integration_id": integration_id,
                        },
                        status="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
        return existing, "updated" if changed else "unchanged"

    row = SafetyEvent(
        alert_id=await next_alert_id(session, timezone),
        samsara_event_id=event.samsara_event_id,
        vehicle_id=vehicle.id if vehicle else None,
        driver_id=driver.id if driver else None,
        event_timestamp=event.event_timestamp,
        received_at=now,
        severity=event.severity,
        primary_behavior=event.primary_behavior,
        behavior_labels_json=event.behaviors,
        context_labels_json=event.contexts,
        event_state=event.event_state,
        latitude=event.latitude,
        longitude=event.longitude,
        location_name=event.location_name,
        media_json=event.media,
        review_status="new",
        coaching_status="pending",
        initialized_baseline=initialization,
        raw_event_json=safe_raw,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    session.add(
        SafetyEventUpdate(
            safety_event_id=row.id,
            event_type="detected",
            received_at=now,
            raw_update_json=safe_raw,
            created_at=now,
        )
    )
    if not initialization or send_initial:
        session.add(
            NotificationOutbox(
                safety_event_id=row.id,
                category="safety",
                notification_type="new",
                idempotency_key=f"safety:new:{event.samsara_event_id}",
                payload_json={"safety_event_id": row.id, "alert_id": row.alert_id, "integration_id": integration_id},
                status="pending",
                attempt_count=0,
                next_attempt_at=now + timedelta(seconds=notification_delay_seconds),
                created_at=now,
            )
        )
    return row, "created"
