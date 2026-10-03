from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class Availability(StrEnum):
    AT_OFFICE = "at_office"
    AT_PG = "at_pg"
    ASK_ME = "ask_me"


class ResidentProfile(BaseModel):
    resident_name: str = Field(default="", max_length=80)
    pg_name: str = Field(default="", max_length=120)
    guard_location: str = Field(default="", max_length=180)
    guard_directions: str = Field(default="", max_length=800)
    office_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    office_start: time = time(9, 0)
    office_end: time = time(19, 0)
    outside_office: Availability = Availability.ASK_ME
    weekend: Availability = Availability.ASK_ME
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"

    @field_validator("resident_name", "pg_name", "guard_location", "guard_directions")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("office_days")
    @classmethod
    def validate_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("Office days must be between Monday (0) and Sunday (6).")
        if len(set(value)) != len(value):
            raise ValueError("Each office day can only be selected once.")
        return sorted(value)

    @field_validator("office_start", "office_end")
    @classmethod
    def validate_time(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Use local hours and minutes for office times.")
        return value

    @model_validator(mode="after")
    def validate_office_hours(self) -> Self:
        if self.office_start >= self.office_end:
            raise ValueError("Office end time must be later than its start time.")
        return self


class TodayOverride(BaseModel):
    status: Availability
    local_date: date


class OverrideRequest(BaseModel):
    status: Availability | None


class DeliveryMode(BaseModel):
    enabled: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.enabled and self.expires_at is None:
            raise ValueError("Choose when delivery mode should end.")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("Delivery mode end time must include a timezone.")
        if not self.enabled:
            self.expires_at = None
        return self


class DeliveryContext(BaseModel):
    availability: Availability
    availability_source: Literal["today", "office_hours", "outside_office", "weekend"]
    availability_explanation: str
    today_override: Availability | None
    setup_complete: bool
    delivery_mode_active: bool
    delivery_mode_expired: bool
    instruction: str
    restrictions: list[str]
    local_date: date


class Dashboard(BaseModel):
    profile: ResidentProfile
    delivery_mode: DeliveryMode
    context: DeliveryContext
    server_time: datetime
    voice_connected: Literal[False] = False
