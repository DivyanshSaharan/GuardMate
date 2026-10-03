from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Availability, DeliveryContext, DeliveryMode, ResidentProfile, TodayOverride

RESTRICTIONS = [
    "Prepaid parcels only",
    "OTP, signature or payment requests need your help",
    "Never leave a parcel unattended without your approval",
]


def build_context(
    profile: ResidentProfile,
    delivery_mode: DeliveryMode,
    override: TodayOverride | None,
    now: datetime,
) -> DeliveryContext:
    local_now = now.astimezone(ZoneInfo(profile.timezone))
    valid_override = override if override and override.local_date == local_now.date() else None
    setup_complete = bool(profile.resident_name and profile.pg_name and profile.guard_location)

    if valid_override:
        availability = valid_override.status
        source = "today"
        explanation = "Your override for today. Tomorrow, your saved routine takes over."
    elif (
        local_now.weekday() in profile.office_days
        and profile.office_start <= local_now.time() < profile.office_end
    ):
        availability = Availability.AT_OFFICE
        source = "office_hours"
        explanation = (
            f"Following your office routine, "
            f"{profile.office_start.strftime('%H:%M')}–{profile.office_end.strftime('%H:%M')} IST."
        )
    elif local_now.weekday() in profile.office_days:
        availability = profile.outside_office
        source = "outside_office"
        explanation = "Following your preference outside office hours."
    else:
        availability = profile.weekend
        source = "weekend"
        explanation = "Following your preference for non-office days."

    expired = bool(
        delivery_mode.enabled
        and delivery_mode.expires_at is not None
        and now >= delivery_mode.expires_at
    )
    active = delivery_mode.enabled and not expired and setup_complete

    if not setup_complete:
        instruction = "Save your name, PG name and guard-room location to prepare instructions."
    elif availability == Availability.AT_OFFICE:
        instruction = (
            f"Please hand the prepaid parcel to security at {profile.guard_location}. "
            "If nobody is there, please contact me before leaving it anywhere else."
        )
        if profile.guard_directions:
            instruction += f" {profile.guard_directions}"
    elif availability == Availability.AT_PG:
        instruction = "I'm at the PG. Please call me so I can receive the parcel personally."
    else:
        instruction = "Please contact me to confirm where to hand over this parcel."

    return DeliveryContext(
        availability=availability,
        availability_source=source,
        availability_explanation=explanation,
        today_override=valid_override.status if valid_override else None,
        setup_complete=setup_complete,
        delivery_mode_active=active,
        delivery_mode_expired=expired,
        instruction=instruction,
        restrictions=RESTRICTIONS,
        local_date=local_now.date(),
    )
