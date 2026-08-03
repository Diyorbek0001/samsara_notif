from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from app.database.models import SafetyEvent


def _vehicle_name(event: SafetyEvent) -> str:
    if not event.vehicle:
        return "Unknown"
    return (
        event.vehicle.unit_number
        or event.vehicle.vehicle_name
        or event.vehicle.license_plate
        or event.vehicle.vin
        or "Unknown"
    )


def safety_message(event: SafetyEvent, timezone: str, test: bool = False) -> str:
    title = (
        "🧪 TEST — NOT A REAL FLEET ALERT"
        if test
        else f"⚠️ {event.severity.upper()} ALERT: {event.primary_behavior.upper()}"
    )
    lines = [
        f"<b>{escape(title)}</b>",
        "",
        f"<b>Alert ID:</b> {escape(event.alert_id)}",
        f"<b>Driver:</b> {escape(event.driver.driver_name if event.driver and event.driver.driver_name else 'Unknown')}",
        f"<b>Vehicle:</b> {escape(_vehicle_name(event))}",
        f"<b>Behavior:</b> {escape(', '.join(event.behavior_labels_json) or event.primary_behavior)}",
        f"<b>Severity:</b> {escape(event.severity.title())}",
        "",
        f"🕒 {escape(event.event_timestamp.astimezone(ZoneInfo(timezone)).strftime('%B %d, %Y — %I:%M %p %Z'))}",
    ]
    if event.location_name:
        lines.append(f"📍 {escape(event.location_name)}")
    if event.latitude is not None and event.longitude is not None:
        maps = f"https://www.google.com/maps/search/?api=1&amp;query={event.latitude:.6f},{event.longitude:.6f}"
        lines.append(f'🗺 <a href="{maps}">Google Maps</a>')
    lines.extend(
        [
            "",
            f"<b>Review status:</b> {escape(event.review_status.title())}",
            f"<b>Coaching status:</b> {escape(event.coaching_status.replace('_', ' ').title())}",
        ]
    )
    return "\n".join(lines)

