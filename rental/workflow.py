from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import StatusHistory, Suit, SuitRequest, record_inventory_movement


WAITING_HOURS = 24
ACTIVE_BOOKING_STATES = ("request", "pending", "waiting", "rented")

FLOW_RENTAL = ("request", "pending", "rented", "returned", "completed")
FLOW_RESERVATION = ("request", "pending", "waiting", "rented", "returned", "completed")

LABELS = {
    "request": "Codsi",
    "pending": "Sugaya",
    "waiting": "Hakad / Sugitaan",
    "rented": "La kiraystay",
    "returned": "La soo celiyay",
    "completed": "Dhammaaday",
    "cancelled": "La joojiyay",
    "expired": "Waqtigii wuu dhacay",
}

ICONS = {
    "request": "fa-file-signature",
    "pending": "fa-hourglass-half",
    "waiting": "fa-clock",
    "rented": "fa-handshake",
    "returned": "fa-rotate-left",
    "completed": "fa-circle-check",
    "cancelled": "fa-xmark",
    "expired": "fa-hourglass-end",
}

NEXT_NOTE = {
    "request": "Reception ayaa codsiga dib u eegaya.",
    "pending": "Waxaa la diyaarinayaa suit-ka iyo lacag-bixinta.",
    "waiting": "Bixi deposit-ka ka hor waqtiga kama dambaysta ah.",
    "rented": "Fadlan suit-ka ku soo celi taariikhda lagu heshiiyey.",
    "returned": "Waxaa la hubinayaa lacagaha iyo xaaladda alaabta.",
    "completed": "",
    "cancelled": "",
    "expired": "",
}

TRANSITIONS = {
    "request": ("pending", "cancelled"),
    "pending": ("waiting", "rented", "cancelled"),
    "waiting": ("rented", "cancelled", "expired"),
    "rented": ("returned",),
    "returned": ("completed",),
    "completed": (),
    "cancelled": (),
    "expired": (),
}

LEGACY_STATUS = {
    "request": "Pending",
    "pending": "Approved",
    "waiting": "Reserved",
    "rented": "Active",
    "returned": "Returned",
    "completed": "Returned",
    "cancelled": "Rejected",
    "expired": "Expired",
}


class WorkflowError(Exception):
    pass


def is_staff_user(user):
    if not user.is_authenticated:
        return False
    from .views import is_admin, is_reception

    return is_admin(user) or is_reception(user)


def user_bookings(user):
    return SuitRequest.objects.filter(user=user)


def has_date_overlap(booking, start_date=None, end_date=None):
    start_date = start_date or booking.requested_start_date
    end_date = end_date or booking.requested_end_date
    if not start_date or not end_date:
        return False
    return SuitRequest.objects.filter(
        suit_id=booking.suit_id,
        booking_type="reservation",
        workflow_status__in=ACTIVE_BOOKING_STATES,
        requested_start_date__lte=end_date,
        requested_end_date__gte=start_date,
    ).exclude(pk=booking.pk).exists()


def validate_booking_dates(suit, start_date, end_date, exclude_booking_id=None):
    today = timezone.localdate()
    if not start_date or not end_date:
        raise WorkflowError("Fadlan geli taariikhda bilowga iyo dhammaadka.")
    if start_date < today:
        raise WorkflowError("Taariikhda bilowgu ma noqon karto maalin hore.")
    if end_date < start_date:
        raise WorkflowError("Taariikhda dhammaadku waa inay la mid ama ka dambaysaa bilowga.")

    overlapping = SuitRequest.objects.filter(
        suit=suit,
        booking_type="reservation",
        workflow_status__in=ACTIVE_BOOKING_STATES,
        requested_start_date__lte=end_date,
        requested_end_date__gte=start_date,
    )
    if exclude_booking_id:
        overlapping = overlapping.exclude(pk=exclude_booking_id)
    if overlapping.exists():
        raise WorkflowError("Suit-kan taariikhahaas booking kale ayaa hore u qabsaday.")


def record_status_change(booking, new_status, changed_by=None, note=""):
    old_status = booking.workflow_status
    if old_status == new_status:
        return
    booking.workflow_status = new_status
    booking.save(update_fields=["workflow_status"])
    StatusHistory.objects.create(
        booking=booking,
        old_status=old_status,
        new_status=new_status,
        changed_by=changed_by,
        note=note[:255],
    )


def _sync_suit_after_release(suit):
    if suit.is_retired or suit.status == "Dry Cleaning":
        return
    active_rentals = SuitRequest.objects.filter(
        suit=suit,
        workflow_status="rented",
    ).exists()
    waiting_reservations = SuitRequest.objects.filter(
        suit=suit,
        workflow_status="waiting",
        waiting_deadline__gt=timezone.now(),
    ).exists()
    if waiting_reservations:
        suit.status = "Reserved"
    elif active_rentals and suit.quantity <= 0:
        suit.status = "Rented"
    else:
        suit.status = "Available"
    suit.save(update_fields=["status"])


@transaction.atomic
def change_status(booking, new_status, user=None, note="", prepared=False, picked_up=False):
    booking = SuitRequest.objects.select_for_update().select_related("suit").get(pk=booking.pk)
    old_status = booking.workflow_status
    if new_status not in TRANSITIONS.get(old_status, ()):
        raise WorkflowError(
            f"Marxaladda {LABELS.get(old_status, old_status)} looma beddeli karo "
            f"{LABELS.get(new_status, new_status)}."
        )

    today = timezone.localdate()
    now = timezone.now()
    suit = Suit.objects.select_for_update().get(pk=booking.suit_id)

    if new_status == "pending":
        booking.status = "Approved"
        booking.approved_at = booking.approved_at or now
        booking.save(update_fields=["status", "approved_at"])
        record_status_change(booking, "pending", user, note or "Codsiga waa la eegay.")
        return booking

    if new_status == "waiting":
        if booking.booking_type != "reservation":
            raise WorkflowError("Kiro caadi ah ma geli karto marxaladda sugitaanka.")
        if not prepared:
            raise WorkflowError("Reception waa inay xaqiijisaa in suit-ka la diyaariyey.")
        if not booking.requested_start_date or not booking.requested_end_date:
            raise WorkflowError("Booking-kan taariikhaha qorshaysan ma laha.")
        if booking.requested_start_date <= today:
            raise WorkflowError("Reservation-ku waa inuu leeyahay taariikh bilow oo mustaqbal ah.")
        if has_date_overlap(booking):
            raise WorkflowError("Suit-kan taariikhahaas booking kale ayaa hore u qabsaday.")
        if suit.is_retired or suit.quantity < (booking.quantity_requested or 1):
            raise WorkflowError("Stock ku filan ma jiro taariikhaha booking-kan.")

        booking.status = "Reserved"
        booking.approved_at = booking.approved_at or now
        booking.waiting_deadline = now + timedelta(hours=WAITING_HOURS)
        booking.due_date = timezone.make_aware(
            datetime.combine(booking.requested_end_date, time.max),
            timezone.get_current_timezone(),
        )
        booking.save(update_fields=["status", "approved_at", "waiting_deadline", "due_date"])
        record_inventory_movement(
            suit,
            "reserved",
            performed_by=user,
            rental_request=booking,
            notes=f"Reservation for {booking.requested_start_date} through {booking.requested_end_date}.",
        )
        record_status_change(booking, "waiting", user, note or "Suit-ka waa la diyaariyey.")
        suit.status = "Reserved"
        suit.save(update_fields=["status"])
        return booking

    if new_status == "rented":
        if not picked_up:
            raise WorkflowError("Xaqiiji in macaamilku suit-ka qaatay.")
        if old_status == "pending" and not prepared:
            raise WorkflowError("Reception waa inay xaqiijisaa in suit-ka la diyaariyey.")
        if booking.booking_type == "reservation":
            if old_status != "waiting":
                raise WorkflowError("Reservation-ku waa inuu marka hore galaa sugitaanka.")
            if not booking.requested_start_date or booking.requested_start_date > today:
                raise WorkflowError("Suit-ka lama bixin karo ka hor taariikhda reservation-ka.")
            if booking.amount_paid <= 0:
                raise WorkflowError("Deposit ama lacag-bixin ayaa loo baahan yahay ka hor qaadashada.")
        elif booking.amount_paid <= 0:
            raise WorkflowError("Lacag-bixin ugu yaraan qayb ah ayaa loo baahan yahay ka hor qaadashada.")

        booking.status = LEGACY_STATUS[old_status]
        booking.save(update_fields=["status"])
        try:
            if not booking.start_rental(start_time=now, performed_by=user):
                raise WorkflowError("Rental-kan mar hore ayaa la bixiyey.")
        except ValidationError as exc:
            raise WorkflowError("; ".join(exc.messages)) from exc
        booking.refresh_from_db()
        record_status_change(booking, "rented", user, note or "Suit-ka macaamilka ayaa la siiyey.")
        return booking

    if new_status == "cancelled":
        if old_status == "rented":
            raise WorkflowError("Rental la bixiyey lama joojin karo; waa in la soo celiyaa.")
        was_reserved = booking.status == "Reserved"
        booking.status = "Rejected"
        booking.waiting_deadline = None
        booking.save(update_fields=["status", "waiting_deadline"])
        if was_reserved:
            record_inventory_movement(
                suit,
                "reservation_released",
                performed_by=user,
                rental_request=booking,
                notes="Reservation cancelled before pickup.",
            )
        record_status_change(booking, "cancelled", user, note or "Booking-ga waa la joojiyey.")
        _sync_suit_after_release(suit)
        return booking

    if new_status == "expired":
        if old_status != "waiting" or booking.amount_paid > 0:
            raise WorkflowError("Sugitaanka waxa la dhamayn karaa marka deadline-ku dhaco oo deposit aanu jirin.")
        booking.status = "Expired"
        booking.waiting_deadline = None
        booking.save(update_fields=["status", "waiting_deadline"])
        record_inventory_movement(
            suit,
            "reservation_released",
            performed_by=user,
            rental_request=booking,
            notes="Reservation hold expired without a deposit.",
        )
        record_status_change(booking, "expired", user, note or "Deadline-ka reservation-ka ayaa dhacay.")
        _sync_suit_after_release(suit)
        return booking

    if new_status == "completed":
        if booking.status != "Returned" or booking.return_date is None:
            raise WorkflowError("Rental-ka waa in marka hore si rasmi ah loo soo celiyaa.")
        if booking.balance_due > 0:
            raise WorkflowError("Balance ayaa weli ka dhiman; dhammaystir lacag-bixinta marka hore.")
        record_status_change(booking, "completed", user, note or "Return-ka iyo xisaabtu waa dhammaadeen.")
        return booking

    raise WorkflowError("Isbeddelka status-ka lama oggola.")


def expire_overdue():
    expired = SuitRequest.objects.filter(
        workflow_status="waiting",
    ).filter(
        Q(waiting_deadline__lte=timezone.now())
        | Q(waiting_deadline__isnull=True, due_date__lte=timezone.now())
    ).select_related("suit")
    for booking in expired:
        if booking.amount_paid <= 0:
            try:
                change_status(booking, "expired", note="Deadline-ka reservation-ka ayaa dhacay.")
            except WorkflowError:
                continue


def allowed_next(booking):
    possible = TRANSITIONS.get(booking.workflow_status, ())
    if booking.workflow_status == "pending":
        if booking.booking_type == "reservation":
            possible = ("waiting", "cancelled")
        else:
            possible = ("rented", "cancelled")
    return [(status, LABELS[status]) for status in possible]


def build_steps(booking):
    flow = FLOW_RESERVATION if booking.booking_type == "reservation" else FLOW_RENTAL
    history_by_status = {}
    for item in booking.status_history.all():
        history_by_status.setdefault(item.new_status, item)

    def make_step(key, state):
        history = history_by_status.get(key)
        return {
            "key": key,
            "label": LABELS[key],
            "icon": ICONS[key],
            "state": state,
            "time": history.changed_at if history else None,
            "note": NEXT_NOTE[key] if state == "current" else "",
        }

    current = booking.workflow_status
    if current in {"cancelled", "expired"}:
        steps = []
        for status in flow:
            if status == current:
                break
            if status in history_by_status:
                steps.append(make_step(status, "done"))
        steps.append(make_step(current, "failed"))
        return steps

    if current not in flow:
        current = "request"
    current_index = flow.index(current)
    steps = []
    for index, status in enumerate(flow):
        if current == "completed" or index < current_index:
            state = "done"
        elif index == current_index:
            state = "current"
        else:
            state = "upcoming"
        steps.append(make_step(status, state))
    return steps
