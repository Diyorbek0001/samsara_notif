from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlparse
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


def samsara_inbox_url(event: SafetyEvent) -> str | None:
    """Samsara's own link to this event in the Safety Inbox, if present."""
    raw = event.raw_event_json if isinstance(event.raw_event_json, dict) else {}
    url = raw.get("inboxEventUrl")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (
        parsed.hostname == "samsara.com" or (parsed.hostname or "").endswith(".samsara.com")
    ):
        return None
    return url


def behavior_label(label: str) -> str:
    """Turn Samsara labels like "LightSpeeding" into "Light Speeding"."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).strip()


def safety_message(event: SafetyEvent, timezone: str, test: bool = False) -> str:
    title = (
        "🧪 TEST — NOT A REAL FLEET ALERT"
        if test
        else (
            f"⚠️ {event.severity.upper()} ALERT: {behavior_label(event.primary_behavior).upper()}"
            if event.severity != "unknown"
            else f"⚠️ SAFETY ALERT: {behavior_label(event.primary_behavior).upper()}"
        )
    )
    lines = [
        f"<b>{escape(title)}</b>",
        "",
        f"<b>Alert ID:</b> {escape(event.alert_id)}",
        f"<b>Driver:</b> {escape(event.driver.driver_name if event.driver and event.driver.driver_name else 'Unassigned')}",
        f"<b>Vehicle:</b> {escape(_vehicle_name(event))}",
        f"<b>Behavior:</b> {escape(', '.join(behavior_label(label) for label in event.behavior_labels_json) or behavior_label(event.primary_behavior))}",
        f"<b>Severity:</b> {escape(event.severity.title())}",
        "",
        f"🕒 {escape(event.event_timestamp.astimezone(ZoneInfo(timezone)).strftime('%B %d, %Y — %I:%M %p %Z'))}",
    ]
    if event.location_name:
        lines.append(f"📍 {escape(event.location_name)}")
    if event.latitude is not None and event.longitude is not None:
        maps = f"https://www.google.com/maps/search/?api=1&amp;query={event.latitude:.6f},{event.longitude:.6f}"
        lines.append(f'🗺 <a href="{maps}">Google Maps</a>')
    raw_info = event.raw_event_json if isinstance(event.raw_event_json, dict) else {}
    snapshot = raw_info.get("snapshotInfo")
    if isinstance(snapshot, dict) and snapshot.get("capturedAt"):
        try:
            captured = datetime.fromisoformat(str(snapshot["capturedAt"]).replace("Z", "+00:00"))
        except ValueError:
            captured = None
        if captured:
            event_time = event.event_timestamp
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            minutes = round(abs((captured - event_time).total_seconds()) / 60)
            when = captured.astimezone(ZoneInfo(timezone)).strftime("%I:%M:%S %p %Z")
            if snapshot.get("afterEvent"):
                relation = "at the event" if minutes == 0 else f"{minutes} min after the event"
            else:
                relation = f"{minutes} min before the event — no newer image available"
            lines.append(f"📸 <b>Camera snapshot:</b> {escape(when)} ({escape(relation)})")
    inbox = samsara_inbox_url(event)
    if inbox:
        lines.append(f'🔗 <a href="{escape(inbox, quote=True)}">Samsara Safety Inbox</a>')
    speed = (
        event.raw_event_json.get("speedingDetails")
        if isinstance(event.raw_event_json, dict)
        else None
    )
    if isinstance(speed, dict) and speed.get("maxSpeedMph") is not None:
        maximum = round(float(speed["maxSpeedMph"]))
        lines.append(f"🚛 <b>Maximum speed:</b> {maximum} mph")
        if speed.get("postedSpeedLimitMph") is not None:
            limit = round(float(speed["postedSpeedLimitMph"]))
            lines.append(f"🚦 <b>Posted limit:</b> {limit} mph")
        if speed.get("overLimitMph") is not None:
            over = round(float(speed["overLimitMph"]))
            lines.append(f"⚡ <b>Over limit:</b> {over} mph")
        if speed.get("durationSeconds") is not None:
            lines.append(f"⏱ <b>Duration:</b> {int(speed['durationSeconds'])} seconds")
    lines.extend(
        [
            "",
            f"<b>Review status:</b> {escape(event.review_status.title())}",
            f"<b>Coaching status:</b> {escape(event.coaching_status.replace('_', ' ').title())}",
        ]
    )
    return "\n".join(lines)
