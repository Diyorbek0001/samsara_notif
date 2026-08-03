from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable

from .models import NormalizedFault


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value).strip() or None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_severity(raw: dict[str, Any]) -> str:
    lamps = raw.get("lamps") if isinstance(raw.get("lamps"), dict) else {}
    stop = _first(raw, "stopLamp", "stopLampStatus") or lamps.get("stop")
    protect = _first(raw, "protectLamp", "protectLampStatus") or lamps.get("protect")
    warning = _first(raw, "warningLamp", "warningLampStatus", "milStatus", "checkEngine") or lamps.get("warning")
    active_values = {True, 1, "on", "On", "ON", "active", "Active"}
    if stop in active_values:
        return "critical"
    if protect in active_values:
        return "high"
    if warning in active_values:
        return "medium"
    return "unknown"


def normalize_fault(vehicle: dict[str, Any], raw: dict[str, Any]) -> NormalizedFault | None:
    vehicle_id = _text(vehicle.get("id"))
    if not vehicle_id:
        return None
    j1939 = raw.get("j1939") if isinstance(raw.get("j1939"), dict) else {}
    obd = raw.get("obdii") if isinstance(raw.get("obdii"), dict) else {}
    oem = raw.get("oem") if isinstance(raw.get("oem"), dict) else {}
    spn = _text(
        _first(raw, "spn", "spnId", "suspectParameterNumber")
        or _first(j1939, "spn", "spnId", "suspectParameterNumber")
    )
    fmi = _text(
        _first(raw, "fmi", "fmiId", "failureModeIdentifier")
        or _first(j1939, "fmi", "fmiId", "failureModeIdentifier")
    )
    obd_code = _text(_first(raw, "obdCode", "obdiiCode", "dtc") or _first(obd, "code", "dtc"))
    manufacturer_code = _text(
        _first(raw, "manufacturerCode", "oemCode") or _first(oem, "code", "dtc")
    )
    protocol = _text(_first(raw, "protocol", "faultType", "type"))
    if spn is not None:
        protocol = "J1939"
        normalized = f"SPN {spn}" + (f" / FMI {fmi}" if fmi is not None else "")
        identity = f"J1939|{spn}|{fmi or '-'}"
    elif obd_code:
        protocol = protocol or "OBD-II"
        obd_code = obd_code.upper()
        normalized, identity = obd_code, f"OBD-II|{obd_code}"
    elif manufacturer_code:
        protocol = protocol or "OEM"
        normalized, identity = manufacturer_code, f"OEM|{manufacturer_code}"
    else:
        raw_code = _text(_first(raw, "code", "id", "name"))
        if not raw_code:
            return None
        protocol = protocol or "UNKNOWN"
        normalized, identity = raw_code, f"{protocol}|{raw_code}"
    fingerprint = hashlib.sha256(f"{vehicle_id}|{identity}".encode()).hexdigest()
    active = _first(raw, "active", "isActive")
    if active is None and _text(raw.get("status")):
        status = _text(raw.get("status")).lower()
        active = status in {"active", "on", "open"} if status in {"active", "on", "open", "inactive", "off", "cleared"} else None
    occurrence = _first(raw, "occurrenceCount", "occurrences", "count")
    try:
        occurrence = int(occurrence) if occurrence is not None else None
    except (TypeError, ValueError):
        occurrence = None
    return NormalizedFault(
        vehicle_id=vehicle_id,
        vehicle_name=_text(vehicle.get("name")),
        protocol=protocol,
        spn=spn,
        fmi=fmi,
        obd_code=obd_code,
        manufacturer_code=manufacturer_code,
        source=_text(_first(raw, "source", "sourceAddressName", "txId", "ecu", "module")),
        normalized_code=normalized,
        definition_identity=identity,
        fingerprint=fingerprint,
        description=_text(
            _first(raw, "description", "spnDescription", "fmiDescription", "label")
        ),
        occurrence_count=occurrence,
        timestamp=parse_time(_first(raw, "time", "timestamp")),
        explicit_active=active if isinstance(active, bool) else None,
        severity=classify_severity(raw),
        raw=raw,
    )


def parse_vehicle_faults(vehicle: dict[str, Any]) -> list[NormalizedFault]:
    events = vehicle.get("faultCodes")
    if not isinstance(events, list):
        return []
    raw_faults: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        j1939 = event.get("j1939")
        codes = j1939.get("diagnosticTroubleCodes") if isinstance(j1939, dict) else None
        if isinstance(codes, list):
            lamps = j1939.get("checkEngineLights")
            for code in codes:
                if isinstance(code, dict):
                    expanded = dict(code)
                    expanded["protocol"] = "J1939"
                    expanded["active"] = True
                    if event.get("time") is not None:
                        expanded["time"] = event["time"]
                    if lamps is not None:
                        expanded["lamps"] = lamps
                    raw_faults.append(expanded)
            continue
        raw_faults.append(event)
    parsed = [normalize_fault(vehicle, item) for item in raw_faults]
    deduped: dict[str, NormalizedFault] = {}
    for fault in parsed:
        if fault is not None:
            deduped[fault.fingerprint] = fault
    return list(deduped.values())


def compare_fault_sets(previous: Iterable[str], current: Iterable[str]) -> tuple[set[str], set[str], set[str]]:
    old, new = set(previous), set(current)
    return new - old, new & old, old - new
