from app.samsara.parsers import classify_severity, compare_fault_sets, normalize_fault, parse_vehicle_faults


def test_j1939_identity_does_not_merge_fmi():
    vehicle = {"id": "v1", "name": "Truck"}
    one = normalize_fault(vehicle, {"spn": 4364, "fmi": 18})
    two = normalize_fault(vehicle, {"spn": 4364, "fmi": 0})
    assert one.definition_identity == "J1939|4364|18"
    assert one.fingerprint != two.fingerprint


def test_obd_identity_is_normalized():
    fault = normalize_fault({"id": "v1"}, {"obdCode": "p0420"})
    assert fault.normalized_code == "P0420"
    assert fault.definition_identity == "OBD-II|P0420"


def test_oem_identity():
    fault = normalize_fault({"id": "v1"}, {"oem": {"code": "OEM-17"}})
    assert fault.definition_identity == "OEM|OEM-17"


def test_deduplicates_same_response():
    payload = {"id": "v1", "faultCodes": [{"spn": 1, "fmi": 2}, {"spn": 1, "fmi": 2}]}
    assert len(parse_vehicle_faults(payload)) == 1


def test_real_samsara_j1939_event_wrapper():
    payload = {
        "id": "v1",
        "name": "Truck",
        "faultCodes": [
            {
                "time": "2026-07-24T14:42:00Z",
                "canBusType": "j1939",
                "j1939": {
                    "checkEngineLights": {"emissionsIsOn": True},
                    "diagnosticTroubleCodes": [
                        {
                            "spnId": 4364,
                            "fmiId": 18,
                            "occurrenceCount": 3,
                            "spnDescription": "Aftertreatment",
                            "sourceAddressName": "Engine",
                        }
                    ],
                },
            }
        ],
    }
    faults = parse_vehicle_faults(payload)
    assert len(faults) == 1
    assert faults[0].definition_identity == "J1939|4364|18"
    assert faults[0].occurrence_count == 3
    assert faults[0].explicit_active is True


def test_set_comparison():
    assert compare_fault_sets({"a", "b"}, {"b", "c"}) == ({"c"}, {"b"}, {"a"})


def test_severity_is_conservative():
    assert classify_severity({"stopLamp": "On"}) == "critical"
    assert classify_severity({"protectLamp": True}) == "high"
    assert classify_severity({"warningLamp": "On"}) == "medium"
    assert classify_severity({}) == "unknown"
