from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    FaultDefinition,
    FaultEvent,
    FaultIncident,
    IntegrationState,
    IssueSequence,
    NotificationOutbox,
    Vehicle,
)
from app.samsara.models import NormalizedFault


def utcnow() -> datetime:
    return datetime.now(UTC)


async def state_get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(IntegrationState, key)
    return row.value if row else None


async def state_set(session: AsyncSession, key: str, value: str) -> None:
    now = utcnow()
    row = await session.get(IntegrationState, key)
    if row:
        row.value, row.updated_at = value, now
    else:
        session.add(IntegrationState(key=key, value=value, updated_at=now))


async def next_issue_id(session: AsyncSession, timezone: str) -> str:
    local_date = utcnow().astimezone(ZoneInfo(timezone)).date().isoformat()
    sequence = await session.get(IssueSequence, local_date)
    if sequence is None:
        sequence = IssueSequence(local_date=local_date, last_value=1)
        session.add(sequence)
        value = 1
    else:
        sequence.last_value += 1
        value = sequence.last_value
    await session.flush()
    return f"FLT-{local_date.replace('-', '')}-{value:04d}"


async def _vehicle(session: AsyncSession, fault: NormalizedFault) -> Vehicle:
    vehicle = await session.scalar(
        select(Vehicle).where(Vehicle.samsara_vehicle_id == fault.vehicle_id)
    )
    now = utcnow()
    if vehicle is None:
        vehicle = Vehicle(
            samsara_vehicle_id=fault.vehicle_id,
            vehicle_name=fault.vehicle_name,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(vehicle)
        await session.flush()
    elif fault.vehicle_name:
        vehicle.vehicle_name, vehicle.updated_at = fault.vehicle_name, now
    return vehicle


async def _definition(session: AsyncSession, fault: NormalizedFault) -> FaultDefinition:
    definition = await session.scalar(
        select(FaultDefinition).where(FaultDefinition.identity_key == fault.definition_identity)
    )
    now = utcnow()
    if definition is None:
        definition = FaultDefinition(
            identity_key=fault.definition_identity,
            protocol=fault.protocol,
            spn=fault.spn,
            fmi=fault.fmi,
            obd_code=fault.obd_code,
            manufacturer_code=fault.manufacturer_code,
            source=fault.source,
            normalized_code=fault.normalized_code,
            samsara_description=fault.description,
            default_severity=fault.severity,
            created_at=now,
            updated_at=now,
        )
        session.add(definition)
        await session.flush()
    return definition


async def _event(
    session: AsyncSession, incident: FaultIncident, kind: str, fault: NormalizedFault
) -> None:
    now = utcnow()
    session.add(
        FaultEvent(
            incident_id=incident.id,
            event_type=kind,
            samsara_timestamp=fault.timestamp,
            received_at=now,
            occurrence_count=fault.occurrence_count,
            raw_event_json=fault.raw,
            created_at=now,
        )
    )


async def _queue(
    session: AsyncSession, incident: FaultIncident, kind: str, event_key: str,
    integration_id: int | None = None,
) -> None:
    now = utcnow()
    session.add(
        NotificationOutbox(
            incident_id=incident.id,
            notification_type=kind,
            idempotency_key=f"{kind}:{incident.issue_id}:{event_key}",
            payload_json={"incident_id": incident.id, "issue_id": incident.issue_id, "integration_id": integration_id},
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
        )
    )


async def process_fault(
    session: AsyncSession,
    fault: NormalizedFault,
    *,
    initialization: bool,
    send_initial: bool,
    send_cleared: bool,
    reopen_window_hours: int,
    timezone: str,
    integration_id: int | None = None,
) -> tuple[FaultIncident, str]:
    vehicle = await _vehicle(session, fault)
    definition = await _definition(session, fault)
    now = utcnow()
    incident = await session.scalar(
        select(FaultIncident)
        .where(
            and_(
                FaultIncident.vehicle_id == vehicle.id,
                FaultIncident.fault_definition_id == definition.id,
            )
        )
        .order_by(FaultIncident.id.desc())
        .limit(1)
    )

    if fault.explicit_active is False:
        if incident is None or incident.diagnostic_status == "cleared":
            raise ValueError("Explicit clear received without a matching active incident")
        incident.diagnostic_status = "cleared"
        incident.cleared_at = now
        incident.last_seen_at = now
        incident.raw_latest_event_json = fault.raw
        incident.updated_at = now
        await _event(session, incident, "cleared", fault)
        if send_cleared and not initialization:
            await _queue(session, incident, "cleared", fault.timestamp.isoformat() if fault.timestamp else str(now), integration_id)
        return incident, "cleared"

    reopened = False
    if incident and incident.diagnostic_status == "cleared":
        within = incident.cleared_at and now - incident.cleared_at <= timedelta(hours=reopen_window_hours)
        if within:
            incident.diagnostic_status = "active"
            incident.maintenance_status = "active"
            incident.cleared_at = None
            reopened = True
        else:
            incident = None

    if incident is None:
        incident = FaultIncident(
            issue_id=await next_issue_id(session, timezone),
            vehicle_id=vehicle.id,
            fault_definition_id=definition.id,
            diagnostic_status="active",
            maintenance_status="active",
            severity=fault.severity,
            initialized_baseline=initialization,
            first_seen_at=now,
            last_seen_at=now,
            occurrence_count=fault.occurrence_count,
            first_samsara_timestamp=fault.timestamp,
            last_samsara_timestamp=fault.timestamp,
            raw_first_event_json=fault.raw,
            raw_latest_event_json=fault.raw,
            created_at=now,
            updated_at=now,
        )
        session.add(incident)
        await session.flush()
        await _event(session, incident, "detected", fault)
        if not initialization or send_initial:
            await _queue(session, incident, "new", fault.timestamp.isoformat() if fault.timestamp else str(now), integration_id)
        return incident, "created"

    old_count = incident.occurrence_count
    incident.last_seen_at = now
    incident.last_samsara_timestamp = fault.timestamp or incident.last_samsara_timestamp
    incident.occurrence_count = fault.occurrence_count
    incident.raw_latest_event_json = fault.raw
    incident.severity = fault.severity
    incident.updated_at = now
    kind = "reopened" if reopened else ("updated" if old_count != fault.occurrence_count else "unchanged")
    if kind != "unchanged":
        await _event(session, incident, kind, fault)
    if reopened:
        await _queue(session, incident, "reopened", fault.timestamp.isoformat() if fault.timestamp else str(now), integration_id)
    return incident, kind
