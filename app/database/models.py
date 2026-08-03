from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def utc_column(nullable: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(primary_key=True)
    samsara_vehicle_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    unit_number: Mapped[str | None] = mapped_column(String(128), index=True)
    vehicle_name: Mapped[str | None] = mapped_column(String(255))
    vin: Mapped[str | None] = mapped_column(String(64), index=True)
    make: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128))
    year: Mapped[str | None] = mapped_column(String(16))
    license_plate: Mapped[str | None] = mapped_column(String(64))
    external_ids_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_latitude: Mapped[float | None] = mapped_column(Float)
    last_longitude: Mapped[float | None] = mapped_column(Float)
    last_location_name: Mapped[str | None] = mapped_column(Text)
    last_location_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class FaultDefinition(Base):
    __tablename__ = "fault_definitions"
    __table_args__ = (UniqueConstraint("identity_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(512))
    protocol: Mapped[str] = mapped_column(String(32))
    spn: Mapped[str | None] = mapped_column(String(64))
    fmi: Mapped[str | None] = mapped_column(String(64))
    obd_code: Mapped[str | None] = mapped_column(String(64))
    manufacturer_code: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(128))
    normalized_code: Mapped[str] = mapped_column(String(255), index=True)
    samsara_description: Mapped[str | None] = mapped_column(Text)
    custom_description: Mapped[str | None] = mapped_column(Text)
    default_severity: Mapped[str] = mapped_column(String(16), default="unknown")
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class FaultIncident(Base):
    __tablename__ = "fault_incidents"
    __table_args__ = (
        Index("ix_incident_active_lookup", "vehicle_id", "fault_definition_id", "diagnostic_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    fault_definition_id: Mapped[int] = mapped_column(ForeignKey("fault_definitions.id"))
    diagnostic_status: Mapped[str] = mapped_column(String(16), default="active")
    maintenance_status: Mapped[str] = mapped_column(String(16), default="active")
    severity: Mapped[str] = mapped_column(String(16), default="unknown")
    initialized_baseline: Mapped[bool] = mapped_column(default=False)
    first_seen_at: Mapped[datetime] = utc_column()
    last_seen_at: Mapped[datetime] = utc_column()
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int | None] = mapped_column(Integer)
    first_samsara_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_samsara_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_by_name: Mapped[str | None] = mapped_column(String(255))
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    repair_notes: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_topic_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    raw_first_event_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_latest_event_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()
    vehicle: Mapped[Vehicle] = relationship()
    definition: Mapped[FaultDefinition] = relationship()


class FaultEvent(Base):
    __tablename__ = "fault_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("fault_incidents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    samsara_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = utc_column()
    occurrence_count: Mapped[int | None] = mapped_column(Integer)
    raw_event_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()


class IntegrationState(Base):
    __tablename__ = "integration_state"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = utc_column()


class SamsaraAccount(Base):
    __tablename__ = "samsara_accounts"
    __table_args__ = (UniqueConstraint("name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    encrypted_api_token: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class TelegramRoute(Base):
    __tablename__ = "telegram_routes"
    __table_args__ = (
        UniqueConstraint("samsara_account_id", "chat_id", "thread_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    samsara_account_id: Mapped[int] = mapped_column(
        ForeignKey("samsara_accounts.id"), index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_title: Mapped[str | None] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()
    samsara_account: Mapped[SamsaraAccount] = relationship()


class IssueSequence(Base):
    __tablename__ = "issue_sequences"
    local_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("fault_incidents.id"), index=True)
    safety_event_id: Mapped[int | None] = mapped_column(ForeignKey("safety_events.id"), index=True)
    category: Mapped[str] = mapped_column(String(16), default="fault", index=True)
    notification_type: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = utc_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationLog(Base):
    __tablename__ = "notification_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("fault_incidents.id"))
    notification_type: Mapped[str] = mapped_column(String(32))
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_topic_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()


class WorkerLease(Base):
    __tablename__ = "worker_leases"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class Driver(Base):
    __tablename__ = "drivers"
    id: Mapped[int] = mapped_column(primary_key=True)
    samsara_driver_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    driver_name: Mapped[str | None] = mapped_column(String(255))
    external_ids_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class SafetySequence(Base):
    __tablename__ = "safety_sequences"
    local_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer)


class SafetyEvent(Base):
    __tablename__ = "safety_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    samsara_event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"), index=True)
    event_timestamp: Mapped[datetime] = utc_column()
    received_at: Mapped[datetime] = utc_column()
    severity: Mapped[str] = mapped_column(String(16), default="unknown")
    primary_behavior: Mapped[str] = mapped_column(String(128))
    behavior_labels_json: Mapped[list[Any]] = mapped_column(JSON)
    context_labels_json: Mapped[list[Any]] = mapped_column(JSON)
    event_state: Mapped[str | None] = mapped_column(String(64))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_name: Mapped[str | None] = mapped_column(Text)
    media_json: Mapped[list[Any]] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(32), default="new")
    coaching_status: Mapped[str] = mapped_column(String(32), default="pending")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_by_name: Mapped[str | None] = mapped_column(String(255))
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_topic_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_media_message_ids_json: Mapped[list[Any] | None] = mapped_column(JSON)
    initialized_baseline: Mapped[bool] = mapped_column(default=False)
    raw_event_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()
    vehicle: Mapped[Vehicle | None] = relationship()
    driver: Mapped[Driver | None] = relationship()


class SafetyEventUpdate(Base):
    __tablename__ = "safety_event_updates"
    id: Mapped[int] = mapped_column(primary_key=True)
    safety_event_id: Mapped[int] = mapped_column(ForeignKey("safety_events.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = utc_column()
    raw_update_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = utc_column()
