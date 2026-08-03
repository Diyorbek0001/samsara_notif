from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from app.database.models import FaultIncident


def local_time(value: datetime | None, timezone: str) -> str:
    if value is None:
        return "Unknown"
    return value.astimezone(ZoneInfo(timezone)).strftime("%B %d, %Y — %I:%M %p %Z")


def truck_name(incident: FaultIncident) -> str:
    vehicle = incident.vehicle
    return vehicle.unit_number or vehicle.vehicle_name or vehicle.vin or vehicle.samsara_vehicle_id


def fault_message(incident: FaultIncident, timezone: str, test: bool = False) -> str:
    definition = incident.definition
    lines = [
        f"<b>Issue ID:</b> {escape(incident.issue_id)}",
        f"<b>Truck:</b> {escape(truck_name(incident))}",
        "",
        f"<b>Severity:</b> {escape(incident.severity.upper())}",
        f"<b>Diagnostic status:</b> {escape(incident.diagnostic_status.title())}",
        f"<b>Maintenance status:</b> {escape(incident.maintenance_status.replace('_', ' ').title())}",
        f"<b>Detected:</b> {escape(local_time(incident.first_seen_at, timezone))}",
        "",
        "<b>Diagnostic code:</b>",
        f"Protocol: {escape(definition.protocol)}",
        escape(definition.normalized_code),
    ]
    if incident.occurrence_count is not None:
        lines.append(f"Occurrence count: {incident.occurrence_count}")
    return "\n".join(lines)


def cleared_message(incident: FaultIncident, timezone: str) -> str:
    return "\n".join(
        [
            "<b>✅ TRUCK FAULT CLEARED</b>",
            "",
            f"<b>Issue ID:</b> {escape(incident.issue_id)}",
            f"<b>Truck:</b> {escape(truck_name(incident))}",
            f"<b>Fault:</b> {escape(incident.definition.normalized_code)}",
            "",
            f"<b>Opened:</b> {escape(local_time(incident.first_seen_at, timezone))}",
            f"<b>Cleared:</b> {escape(local_time(incident.cleared_at, timezone))}",
            "<b>Final diagnostic status:</b> Cleared",
        ]
    )
