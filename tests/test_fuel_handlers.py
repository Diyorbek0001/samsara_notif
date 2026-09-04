from app.bot.fuel_handlers import fuel_snapshot_message, parse_fuel_snapshot


def test_fuel_snapshot_sorts_low_to_high_and_missing_last() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck 30",
                "gps": [{"latitude": 40.1, "longitude": -74.2}],
                "fuelPercents": [{"value": 80}],
            },
            {
                "name": "Truck 10",
                "gps": [{"latitude": 41.1, "longitude": -75.2}],
                "fuelPercents": [{"value": 25}],
            },
            {
                "name": "Truck 20",
                "gps": [{"latitude": 42.1, "longitude": -76.2}],
                "fuelPercents": [],
            },
        ]
    )

    assert [row.unit_name for row in rows] == ["Truck 10", "Truck 30", "Truck 20"]


def test_fuel_snapshot_message_lists_coordinates_and_fuel() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck <1>",
                "gps": [{"latitude": "40.1234567", "longitude": "-74.7654321"}],
                "fuelPercents": [{"value": "12.5"}],
            }
        ]
    )

    text = fuel_snapshot_message(rows)

    assert "Truck &lt;1&gt;" in text
    assert "12.5%" in text
    assert "40.123457, -74.765432" in text


def test_fuel_snapshot_accepts_singular_and_nested_fuel_fields() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck 40",
                "gps": {"lat": "40.1", "lng": "-74.2"},
                "fuelPercent": {"time": "2026-09-05T00:00:00Z", "value": {"percent": 44}},
            },
            {
                "name": "Truck 50",
                "gps": {"latitude": 41.1, "longitude": -75.2},
                "fuelLevelPercent": [{"time": "2026-09-05T00:00:00Z", "fuelPercentage": "9"}],
            },
        ]
    )

    assert [(row.unit_name, row.fuel_percent) for row in rows] == [
        ("Truck 50", 9.0),
        ("Truck 40", 44.0),
    ]
