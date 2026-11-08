"""Safety alert types, each delivered to its own forum topic.

A route purpose of ``s:<key>`` receives only safety events of that type.
Keys are short because ``telegram_routes.purpose`` is VARCHAR(16).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyTopic:
    key: str
    title: str
    icon_color: int
    behaviors: frozenset[str]  # normalized: no spaces, lower case

    @property
    def purpose(self) -> str:
        return f"s:{self.key}"


# Telegram allows only these six topic icon colors.
_BLUE, _YELLOW, _PURPLE, _GREEN, _PINK, _RED = (
    0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F,
)

SAFETY_TOPICS: tuple[SafetyTopic, ...] = (
    SafetyTopic("speeding", "🏎 Speeding", _RED, frozenset({
        "lightspeeding", "moderatespeeding", "heavyspeeding", "severespeeding",
        "maxspeed", "speeding",
    })),
    SafetyTopic("phone", "📵 Phone Usage", _PINK, frozenset({"mobileusage"})),
    SafetyTopic("seatbelt", "🔒 Seatbelt", _YELLOW, frozenset({"noseatbelt"})),
    SafetyTopic("camera", "📷 Camera Obstruction", _PURPLE, frozenset({
        "obstructedcamera", "cameraobstruction",
    })),
    SafetyTopic("drowsy", "😴 Drowsiness", _BLUE, frozenset({"drowsy"})),
    SafetyTopic("distraction", "👀 Distraction", _PINK, frozenset({
        "genericdistraction", "drinking", "eating", "smoking",
    })),
    SafetyTopic("following", "🚗 Following Distance", _YELLOW, frozenset({
        "generictailgating", "followingdistance", "tailgating",
    })),
    SafetyTopic("harsh", "🛑 Harsh Driving", _GREEN, frozenset({
        "braking", "harshbraking", "acceleration", "harshacceleration", "harshturn",
    })),
    SafetyTopic("crash", "🚨 Crash & Collision", _RED, frozenset({
        "crash", "harshimpact", "rolloverprotection", "forwardcollisionwarning",
        "nearcollision", "nearcollison",
    })),
    SafetyTopic("traffic", "🚦 Traffic Violations", _GREEN, frozenset({
        "rollingstop", "ranredlight", "leftturn", "uturn", "illegalturn",
    })),
    # Everything Samsara sends that isn't listed above.
    SafetyTopic("other", "📋 Other Safety", _BLUE, frozenset()),
)

OTHER = SAFETY_TOPICS[-1]
_BY_BEHAVIOR = {behavior: topic for topic in SAFETY_TOPICS for behavior in topic.behaviors}


def topic_for(primary_behavior: str | None) -> SafetyTopic:
    """The topic an event belongs to, decided by its primary behavior."""
    key = (primary_behavior or "").replace(" ", "").lower()
    return _BY_BEHAVIOR.get(key, OTHER)
