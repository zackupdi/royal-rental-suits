from django.http import Http404, HttpResponse, JsonResponse
import csv
import base64
import json
import re
import calendar
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError

from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.paginator import Paginator
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F, IntegerField, OuterRef, Q, Subquery, Sum, Value, Count
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.utils.dateparse import parse_date, parse_datetime
from django.core.mail import send_mail
from django.template.loader import render_to_string
from datetime import datetime, time, timedelta, date
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from . import views_public
from .forms import validate_image_upload
from .models import Suit, SuitRequest, Payment, Expense, Notification, FavoriteSuit, Profile, ContactMessage, Staff, Payroll, DryCleaning, InventoryMovement, RentalPolicy, record_inventory_movement, record_late_fee_transaction
from .sms import send_sms
from .workflow import (
    LABELS as WORKFLOW_LABELS,
    WorkflowError,
    allowed_next,
    build_steps,
    change_status,
    expire_overdue,
    is_staff_user,
    record_status_change,
    user_bookings,
    validate_booking_dates,
)
from .pricing import ACCESSORY_TYPES, ALWAYS_CHARGED, calculate_booking, settle_guarantee

ACCESSORY_DAILY_PRICE = Decimal("5.00")


def _available_inventory_queryset(queryset=None):
    reservations = SuitRequest.objects.filter(
        suit_id=OuterRef("pk"),
        status="Reserved",
    ).filter(
        Q(due_date__isnull=True) | Q(due_date__gt=timezone.now())
    ).order_by().values("suit_id").annotate(
        total=Sum("quantity_requested")
    ).values("total")
    return (
        (queryset if queryset is not None else Suit.objects.all())
        .annotate(
            _held_units=Coalesce(
                Subquery(reservations, output_field=IntegerField()),
                Value(0),
            )
        )
        .annotate(_available_units=F("quantity") - F("_held_units"))
        .filter(_available_units__gt=0)
        .exclude(condition_grade="retired")
    )


def _set_inventory_quantity(request, suit, raw_quantity):
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid whole-number stock quantity.")
        return False

    if quantity < 0:
        messages.error(request, "Stock quantity cannot be negative.")
        return False

    with transaction.atomic():
        locked_suit = Suit.objects.select_for_update().get(pk=suit.pk)
        held_quantity = locked_suit.held_quantity
        if quantity < held_quantity:
            messages.error(
                request,
                f"Stock cannot be reduced below the {held_quantity} unit(s) currently reserved.",
            )
            return False

        quantity_delta = quantity - locked_suit.quantity
        if quantity_delta:
            locked_suit.quantity = quantity
            locked_suit.save(update_fields=["quantity"])
            record_inventory_movement(
                locked_suit,
                "adjustment",
                quantity_delta=quantity_delta,
                performed_by=request.user,
                notes="Manual inventory quantity adjustment.",
            )
        suit.quantity = quantity
    return True


def get_user_profile(user):
    if not user or not user.is_authenticated:
        return None
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def is_cashier(user):
    if not user.is_authenticated or not user.is_staff or user.is_superuser:
        return False
    profile = get_user_profile(user)
    return bool(profile and profile.is_cashier)


def is_reception(user):
    if not user.is_authenticated or not user.is_staff or user.is_superuser:
        return False
    profile = get_user_profile(user)
    return bool(
        (profile and profile.is_reception)
        or user.groups.filter(name="Reception").exists()
    )


def is_admin(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if not user.is_staff:
        return False

    profile = get_user_profile(user)
    if (
        (profile and (profile.is_cashier or profile.is_reception)) or
        user.groups.filter(name__in=("Cashier", "Reception")).exists()
    ):
        return False

    return True


def is_reception_or_cashier_or_admin(user):
    return is_admin(user)


def is_cashier_or_admin(user):
    return is_admin(user)


def is_reception_or_admin(user):
    return is_admin(user)


def _can_manage_customer_rentals(user):
    return is_admin(user) or is_reception(user)


def _get_rental_customer_or_404(user_id):
    customer_user = get_object_or_404(User, pk=user_id)
    if customer_user.is_staff and not SuitRequest.objects.filter(user=customer_user).exists():
        raise Http404("This staff account has no customer rental history.")
    return customer_user


def validate_name_input(value, field_name='name'):
    value = (value or '').strip()
    if not value:
        raise ValueError(f"{field_name.replace('_', ' ').capitalize()} is required.")
    if not re.match(r"^[A-Za-z0-9 .,&()'\"-]+$", value):
        raise ValueError(f"{field_name.replace('_', ' ').capitalize()} contains invalid characters.")
    return value


def validate_phone_number(phone):
    phone = (phone or '').strip()
    if not phone:
        raise ValueError("Phone number is required.")
    if not re.match(r'^[0-9+()\- ]{7,20}$', phone):
        raise ValueError("Please enter a valid phone number.")
    return phone


def validate_national_id(national_id):
    national_id = (national_id or '').strip()
    if national_id and not re.match(r'^[A-Za-z0-9\- ]{4,50}$', national_id):
        raise ValueError("Please enter a valid national ID.")
    return national_id


def parse_historical_date(value, field_name="date"):
    """Parse a date used for historical records and reject dates after today."""
    if not value:
        return None, None

    parsed_date = parse_date(value)
    if parsed_date is None:
        return None, f"Enter a valid {field_name}."
    if parsed_date > timezone.localdate():
        return None, f"{field_name.capitalize()} cannot be in the future."
    return parsed_date, None


@login_required
def payments(request):
    if is_admin(request.user):
        return admin_payments(request)

    messages.error(request, "Access denied.")
    return redirect("home")


# =========================
# EMAIL UTILITIES
# =========================
def send_return_reminder_email(suit_request):
    """Send email reminder to customer about suit return deadline"""
    try:
        # Get customer email from suit_request or from user account
        customer_email = suit_request.customer_email or (suit_request.user.email if suit_request.user else None)
        
        if not customer_email:
            return False, "No email address found for customer"
        
        item_name = suit_request.suit.suit_name if suit_request.suit_id else "accessories"
        subject = f"Rental Return Reminder - {item_name}"
        
        # Build email content based on status
        if suit_request.is_overdue:
            status_message = f"Your rental period ended on {suit_request.end_time.strftime('%d %B %Y at %H:%M')}. Please return the suit immediately."
            amount = suit_request.late_fee
            fee_message = f"Late fee of ${amount} is being charged per hour."
        else:
            remaining = suit_request.time_remaining
            days = remaining.days
            hours = remaining.seconds // 3600
            status_message = f"Your rental period ends on {suit_request.end_time.strftime('%d %B %Y at %H:%M')} ({days} days, {hours} hours remaining)."
            fee_message = "Please return the suit on time to avoid late fees."
        
        context = {
            'customer_name': suit_request.name,
            'suit_name': item_name,
            'quantity': suit_request.quantity_requested,
            'phone': suit_request.phone,
            'status_message': status_message,
            'fee_message': fee_message,
            'is_overdue': suit_request.is_overdue,
        }
        
        html_message = render_to_string('rental/email_return_reminder.html', context)
        
        send_mail(
            subject=subject,
            message=f"Dear {suit_request.name},\n\n{status_message}\n\n{fee_message}\n\nPlease contact us at {settings.EMAIL_HOST_USER} if you have any questions.",
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[customer_email],
            html_message=html_message,
            fail_silently=False,
        )
        
        return True, "Email sent successfully"
    except Exception as e:
        return False, str(e)


# =========================
# HOME
# =========================
def home(request):
    return views_public.home(request)


def service_page(request):
    return views_public.service_page(request)


def contact_page(request):
    return views_public.contact_page(request)


def about_page(request):
    return views_public.about_page(request)


# =========================
# ALL SUITS
# =========================
def all_suits(request):
    return views_public.all_suits(request)


# =========================
# BOOK SUIT (ONLY LOGGED IN USERS)
# =========================
@login_required(login_url='customer_login')
def book_suit(request):
    return views_public.book_suit(request)


# =========================
# REQUEST PAGE BOOKING (OPTIONAL FORM)
# =========================
@login_required(login_url='customer_login')
def request_suit(request, suit_id):
    return views_public.request_suit(request, suit_id)


# =========================
# LOGIN / LOGOUT
# =========================
def customer_login(request):
    return views_public.customer_login(request)


@login_required(login_url='customer_login')
def customer_history(request):
    return views_public.customer_history(request)


def login_view(request):
    return views_public.login_view(request)


def customer_logout(request):
    return views_public.customer_logout(request)


def logout_view(request):
    return views_public.logout_view(request)


# =========================
# DASHBOARD
# =========================
@never_cache
@login_required
def dashboard(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    selected_period = request.GET.get("period", "monthly").lower()
    if selected_period not in {"daily", "weekly", "monthly", "yearly"}:
        selected_period = "monthly"

    selected_category = request.GET.get("category", "all").lower()
    selected_status = request.GET.get("status", "all").lower()

    suits_qs = Suit.objects.all()
    requests_qs = SuitRequest.objects.select_related("suit").order_by("-request_date")

    if selected_category != "all":
        category_terms = {
            "suits": ["suit", "suits"],
            "shirts": ["shirt", "shirts"],
            "ties": ["tie", "ties"],
            "shoes": ["shoe", "shoes"],
            "belts": ["belt", "belts"],
        }
        terms = category_terms.get(selected_category, [])
        if terms:
            category_filter = Q()
            for term in terms:
                category_filter |= Q(suit_name__icontains=term) | Q(description__icontains=term)
            suits_qs = suits_qs.filter(category_filter)
            if suits_qs.exists():
                requests_qs = requests_qs.filter(suit__in=suits_qs)
            else:
                requests_qs = requests_qs.none()

    if selected_status != "all":
        if selected_status == "pending":
            requests_qs = requests_qs.filter(status="Pending")
        elif selected_status == "approved":
            requests_qs = requests_qs.filter(status="Approved")
        elif selected_status == "rented":
            requests_qs = requests_qs.filter(status="Active")
        elif selected_status == "returned":
            requests_qs = requests_qs.filter(status="Returned")
        elif selected_status == "available":
            suits_qs = _available_inventory_queryset(suits_qs)
            requests_qs = requests_qs.filter(suit__in=suits_qs)
        elif selected_status == "dry-cleaning":
            suits_qs = suits_qs.filter(status="Dry Cleaning")
            requests_qs = requests_qs.filter(suit__status="Dry Cleaning")

    suits = suits_qs.order_by("-id")[:10]
    recent_returns = requests_qs.filter(status="Returned").order_by("-return_date")[:5]
    total_payments = Payment.objects.filter(rental__in=requests_qs).count()
    total_revenue = Payment.objects.filter(rental__in=requests_qs).aggregate(total=Sum('amount_paid'))['total'] or 0
    pending_balance_requests = requests_qs.filter(status="Active").exclude(is_paid=True).count()

    now = timezone.now()
    if selected_period == "daily":
        labels = []
        revenue_data = []
        for i in range(7):
            bucket_date = (now.date() - timedelta(days=6 - i))
            labels.append(bucket_date.strftime('%a %d'))
            revenue_data.append(float(Payment.objects.filter(rental__in=requests_qs, payment_date__date=bucket_date).aggregate(total=Sum('amount_paid'))['total'] or 0))
    elif selected_period == "weekly":
        labels = []
        revenue_data = []
        for i in range(8):
            week_start = (now.date() - timedelta(days=now.weekday() + (7 - i) * 7))
            week_end = week_start + timedelta(days=6)
            labels.append(f"Wk {i + 1}")
            revenue_data.append(float(Payment.objects.filter(rental__in=requests_qs, payment_date__date__range=[week_start, week_end]).aggregate(total=Sum('amount_paid'))['total'] or 0))
    elif selected_period == "yearly":
        labels = []
        revenue_data = []
        for i in range(5):
            year = now.year - (4 - i)
            labels.append(str(year))
            revenue_data.append(float(Payment.objects.filter(rental__in=requests_qs, payment_date__year=year).aggregate(total=Sum('amount_paid'))['total'] or 0))
    else:
        labels = []
        revenue_data = []
        for i in range(6):
            month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
            month = (month_start.month - (5 - i)) % 12 or 12
            year = month_start.year if month <= month_start.month else month_start.year - 1
            if month < 1:
                month = 12
                year -= 1
            bucket = datetime(year, month, 1)
            labels.append(bucket.strftime('%b'))
            revenue_data.append(float(Payment.objects.filter(rental__in=requests_qs, payment_date__year=bucket.year, payment_date__month=bucket.month).aggregate(total=Sum('amount_paid'))['total'] or 0))

    period_start = now.date()
    period_end = now.date()
    if selected_period == "weekly":
        period_start = now.date() - timedelta(days=now.weekday())
        period_end = period_start + timedelta(days=6)
    elif selected_period == "monthly":
        period_start = now.replace(day=1).date()
        period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    elif selected_period == "yearly":
        period_start = datetime(now.year, 1, 1).date()
        period_end = datetime(now.year, 12, 31).date()

    period_revenue = Payment.objects.filter(rental__in=requests_qs, payment_date__date__range=[period_start, period_end]).aggregate(total=Sum('amount_paid'))['total'] or 0
    period_expenses = Expense.objects.filter(expense_date__date__range=[period_start, period_end]).aggregate(total=Sum('amount'))['total'] or 0
    period_profit = period_revenue - period_expenses

    active_requests = requests_qs.filter(status="Active")
    pending_requests = requests_qs.filter(status="Pending")
    overdue_requests = [r for r in active_requests if r.is_overdue]
    recent_transactions = Payment.objects.filter(rental__in=requests_qs).select_related('rental__suit').order_by('-payment_date')[:10]

    busiest_days = []
    for entry in requests_qs.values('request_date__date').annotate(count=Count('id')).order_by('-count')[:10]:
        busy_date = entry['request_date__date']
        busiest_days.append((busy_date.strftime('%b %d, %Y') if busy_date else 'Unknown', entry['count']))

    top_suits_data = []
    for entry in requests_qs.values('suit__suit_name').annotate(rental_count=Count('id')).order_by('-rental_count')[:5]:
        top_suits_data.append({
            'suit': type('S', (), {'suit_name': entry['suit__suit_name'], 'category': None, 'status': 'N/A'})(),
            'rental_count': entry['rental_count'],
            'revenue': 0,
        })

    top_suits_labels = [item['suit'].suit_name for item in top_suits_data]
    top_suits_counts = [item['rental_count'] for item in top_suits_data]

    least_rented_suits = []
    for suit in suits_qs.order_by('rental_count')[:5]:
        least_rented_suits.append({
            'suit': suit,
            'rental_count': suit.rental_count,
        })

    context = {
        "suits": suits,
        "total_suits": suits_qs.count(),
        "total_suits_count": suits_qs.count(),
        "total_quantity": suits_qs.aggregate(total=Sum('quantity'))['total'] or 0,
        "available_quantity": suits_qs.filter(status="Available").aggregate(total=Sum('quantity'))['total'] or 0,
        "available_suits_count": _available_inventory_queryset(suits_qs).count(),
        "booked_suits": requests_qs.filter(status="Active").aggregate(total=Sum('quantity_requested'))['total'] or 0,
        "total_requests": requests_qs.count(),
        "pending_requests": pending_requests[:5],
        "recent_requests": requests_qs.filter(status="Active").order_by("-request_date")[:5],
        "recent_returns": recent_returns,
        "pending_balance_requests": pending_balance_requests,
        "total_payments": total_payments,
        "total_revenue": total_revenue,
        "pending": pending_requests.count(),
        "active_rentals": active_requests.order_by("-request_date"),
        "active_count": active_requests.count(),
        "pending_count": pending_requests.count(),
        "overdue_requests": overdue_requests,
        "overdue_count": len(overdue_requests),
        "cleaning_count": suits_qs.filter(status="Dry Cleaning").count(),
        "period_revenue": period_revenue,
        "period_profit": period_profit,
        "selected_period": selected_period,
        "selected_category": selected_category,
        "selected_status": selected_status,
        "period_labels": labels,
        "period_labels_json": json.dumps(labels),
        "period_revenue_data": revenue_data,
        "period_revenue_data_json": json.dumps(revenue_data),
        "status_distribution": {
            "active": active_requests.count(),
            "available": suits_qs.filter(status="Available").count(),
            "overdue": len(overdue_requests),
            "cleaning": suits_qs.filter(status="Dry Cleaning").count(),
        },
        "top_suits_labels": json.dumps(top_suits_labels),
        "top_suits_counts": json.dumps(top_suits_counts),
        "busiest_days": busiest_days,
        "top_suits": top_suits_data,
        "least_rented_suits": least_rented_suits,
        "recent_transactions": recent_transactions,
        "total_customers": User.objects.filter(is_staff=False).count(),
        "total_historic_rentals": requests_qs.count(),
    }

    return render(request, "admin/dashboard.html", context)


@login_required
def cashier_dashboard(request):
    return redirect("dashboard")


@login_required
def reception_dashboard(request):
    if not (is_reception(request.user) or is_cashier(request.user)):
        messages.error(request, "Access denied. Reception staff only.")
        return redirect("dashboard")

    suits_qs = Suit.objects.all()
    pending_requests = SuitRequest.objects.filter(status="Pending").order_by("-request_date")
    active_rentals = SuitRequest.objects.filter(status="Active").order_by("-request_date")
    overdue_requests = [r for r in active_rentals if r.is_overdue]

    today = timezone.now().date()
    today_revenue = Payment.objects.filter(payment_date__date=today).aggregate(total=Sum('amount_paid'))['total'] or 0
    cleaning_count = suits_qs.filter(status="Dry Cleaning").count()

    context = {
        "pending_requests": pending_requests,
        "active_rentals": active_rentals,
        "overdue_requests": overdue_requests,
        "pending_count": pending_requests.count(),
        "active_count": active_rentals.count(),
        "overdue_count": len(overdue_requests),
        "total_suits_count": suits_qs.count(),
        "available_suits_count": _available_inventory_queryset(suits_qs).count(),
        "cleaning_count": cleaning_count,
        "today_revenue": today_revenue,
    }

    return render(request, "reception/reception_dashboard.html", context)


@login_required
def reception_home(request):
    if not (is_reception(request.user) or is_cashier(request.user)):
        messages.error(request, "Access denied. Reception staff only.")
        return redirect("dashboard")

    pending_count = SuitRequest.objects.filter(status="Pending").count()
    active_rentals = SuitRequest.objects.filter(status="Active").order_by("-request_date")
    overdue_requests = [r for r in active_rentals if r.is_overdue]
    total_suits = Suit.objects.count()
    available_count = _available_inventory_queryset().aggregate(
        total=Sum("_available_units")
    )["total"] or 0

    context = {
        'pending_count': pending_count,
        'active_count': active_rentals.count(),
        'overdue_count': len(overdue_requests),
        'total_suits': total_suits,
        'available_count': available_count,
        'recent_requests': active_rentals[:5],
    }

    return render(request, "reception/reception.html", context)


@login_required
def reception_profile(request):
    if not (is_reception(request.user) or is_cashier(request.user)):
        messages.error(request, "Access denied. Reception staff only.")
        return redirect("dashboard")

    profile = get_user_profile(request.user)
    return render(request, "reception/reception_profile.html", {
        'user': request.user,
        'profile': profile,
    })


def _build_booking_desk_context():
    pending_requests = SuitRequest.objects.filter(status="Pending").order_by("-request_date")
    active_rentals = SuitRequest.objects.filter(status="Active").order_by("end_time")
    overdue_requests = [r for r in active_rentals if r.is_overdue]

    today = timezone.now().date()
    available_suits = _available_inventory_queryset().order_by("suit_name")
    returned_requests = SuitRequest.objects.filter(status="Returned").select_related("suit").order_by("-return_date")
    today_new_rentals = SuitRequest.objects.filter(request_date__date=today).count()
    today_returns = SuitRequest.objects.filter(status="Returned", return_date__date=today).count()
    today_revenue = Payment.objects.filter(payment_date__date=today).aggregate(total=Sum('amount_paid'))['total'] or 0
    pending_balances = SuitRequest.objects.filter(status="Active", is_paid=False).aggregate(total=Sum('total_amount'))['total'] or 0
    net_today = today_revenue
    inventory_total = Suit.objects.count()
    available_stock_units = _available_inventory_queryset().aggregate(
        total=Sum("_available_units")
    )["total"] or 0
    dry_cleaning_count = Suit.objects.filter(status="Dry Cleaning").count() + returned_requests.count()
    monthly_cleaning_spend = Expense.objects.filter(expense_date__year=today.year, expense_date__month=today.month).aggregate(total=Sum('amount'))['total'] or 0
    recent_expenses = Expense.objects.order_by("-expense_date")[:5]

    return {
        'pending_requests': pending_requests,
        'active_rentals': active_rentals,
        'overdue_requests': overdue_requests,
        'pending_count': pending_requests.count(),
        'active_count': active_rentals.count(),
        'overdue_count': len(overdue_requests),
        'available_suits': available_suits,
        'rental_day_options': [(days, days * 24) for days in range(1, 31)],
        'today_new_rentals': today_new_rentals,
        'today_returns': today_returns,
        'today_revenue': today_revenue,
        'pending_balances': pending_balances,
        'net_today': net_today,
        'inventory_total': inventory_total,
        'available_stock_units': available_stock_units,
        'dry_cleaning_count': dry_cleaning_count,
        'dry_cleaning_queue': returned_requests[:6],
        'monthly_cleaning_spend': monthly_cleaning_spend,
        'recent_expenses': recent_expenses,
    }


@login_required
def reception_reception(request):
    if not (is_reception(request.user) or is_cashier(request.user)):
        messages.error(request, "Access denied. Reception staff only.")
        return redirect("dashboard")

    prefill = request.session.pop("prefill_customer", None) or {}
    context = _build_booking_desk_context()
    context.update({
        "accessory_daily_price": ACCESSORY_DAILY_PRICE,
        "accessory_types": ACCESSORY_TYPES,
        "always_charged_accessories": ALWAYS_CHARGED,
        "prefill_first_name": prefill.get("first_name", ""),
        "prefill_father_name": prefill.get("father_name", ""),
        "prefill_grandfather_name": prefill.get("grandfather_name", ""),
        "prefill_phone": prefill.get("phone", ""),
        "prefill_present_address": prefill.get("present_address", ""),
        "prefill_parents_address": prefill.get("parents_address", ""),
        "prefill_national_id": prefill.get("national_id", ""),
        "prefill_customer_id": prefill.get("customer_id", ""),
    })
    return render(request, "reception/reception.html", context)


@login_required
def admin_booking(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("dashboard" if is_reception(request.user) or is_cashier(request.user) else "home")

    prefill = request.session.pop("prefill_customer", None) or {}
    context = _build_booking_desk_context()
    context.update({
        "accessory_daily_price": ACCESSORY_DAILY_PRICE,
        "accessory_types": ACCESSORY_TYPES,
        "always_charged_accessories": ALWAYS_CHARGED,
        "prefill_first_name": prefill.get("first_name", ""),
        "prefill_father_name": prefill.get("father_name", ""),
        "prefill_grandfather_name": prefill.get("grandfather_name", ""),
        "prefill_phone": prefill.get("phone", ""),
        "prefill_present_address": prefill.get("present_address", ""),
        "prefill_parents_address": prefill.get("parents_address", ""),
        "prefill_national_id": prefill.get("national_id", ""),
        "prefill_customer_id": prefill.get("customer_id", ""),
    })
    return render(request, "admin/admin_booking.html", context)


@login_required
def all_suits_reception(request):
    """Reception-facing view that embeds the public showroom for quick staff access.

    This keeps the public `all_suits` template as the canonical showroom while
    providing a staff wrapper (sidebar, header) via `base_admin.html`.
    """
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    return render(request, "reception/all_suits_reception.html")


@login_required
def create_rental(request):
    is_repeat_submission = request.POST.get("repeat_rental_submission") == "1"
    customer_user_id = request.POST.get("customer_user_id", "").strip()
    is_reception_user = is_reception(request.user)
    if not is_admin(request.user) and not (is_reception_user and is_repeat_submission):
        messages.error(request, "Only Admin or Reception may create this rental.")
        return redirect("home")

    if request.method != "POST":
        return redirect("manage_users" if is_reception_user else "requests")

    repeat_customer = None
    repeat_token_key = None
    if is_repeat_submission:
        try:
            repeat_customer = _get_rental_customer_or_404(int(customer_user_id))
        except (TypeError, ValueError, Http404):
            messages.error(request, "Select a valid customer account before creating a rental.")
            return redirect("manage_users")

        repeat_token_key = f"repeat_rental_token_{repeat_customer.pk}"
        submitted_token = request.POST.get("rental_submission_token", "")
        if not submitted_token or submitted_token != request.session.get(repeat_token_key):
            messages.error(request, "This rental form was already submitted or has expired. Please review it and try again.")
            return redirect("new_rent_for_customer", user_id=repeat_customer.pk)
        request.session.pop(repeat_token_key, None)

    def rental_error_redirect():
        if repeat_customer:
            field_names = (
                "first_name", "father_name", "grandfather_name", "phone",
                "present_address", "parents_address", "has_national_id",
                "id_number", "suit_id", "suit_price", "quantity", "days", "start_date",
                "end_date", "amount_paid", "payment_method",
                "payment_reference", "notes",
            )
            request.session[f"repeat_rental_form_values_{repeat_customer.pk}"] = {
                name: request.POST.get(name, "") for name in field_names
            }
            return redirect("new_rent_for_customer", user_id=repeat_customer.pk)
        if is_reception_user:
            return redirect("manage_users")
        return redirect("admin_booking")

    suit_id_raw = request.POST.get("suit_id", "").strip()
    suit = None
    if suit_id_raw:
        try:
            suit_id = int(suit_id_raw)
        except (TypeError, ValueError):
            messages.error(request, "Please select a valid suit before creating a rental.")
            return rental_error_redirect()

        suit = Suit.objects.filter(pk=suit_id).first()
        if suit is None:
            messages.error(request, "The selected suit is unavailable. Please choose another suit.")
            return rental_error_redirect()
    try:
        quantity_requested = int(request.POST.get("quantity", 1))
        days_requested = int(request.POST.get("days", 1))
    except (TypeError, ValueError):
        messages.error(request, "Please enter valid numeric values for quantity and rental days.")
        return rental_error_redirect()
    start_date = parse_date(request.POST.get("start_date"))
    end_date = parse_date(request.POST.get("end_date"))
    first_name = request.POST.get("first_name", "").strip()
    father_name = request.POST.get("father_name", "").strip()
    grandfather_name = request.POST.get("grandfather_name", "").strip()
    phone = request.POST.get("phone", "").strip()
    national_id = request.POST.get("id_number") if request.POST.get("has_national_id") else None
    present_address = request.POST.get("present_address", "").strip()
    parents_address = request.POST.get("parents_address", "").strip()
    notes = request.POST.get("notes", "").strip()
    amount_paid_raw = request.POST.get("amount_paid", "0").strip() or "0"
    payment_method = request.POST.get("payment_method", "cash").strip()
    payment_reference = request.POST.get("payment_reference", "").strip()
    accessories = list(dict.fromkeys(request.POST.getlist("addons[]")))
    if any(accessory not in ACCESSORY_TYPES for accessory in accessories):
        messages.error(request, "Please select valid accessories.")
        return rental_error_redirect()
    if suit is None and not accessories:
        messages.error(request, "Select a suit or at least one accessory.")
        return rental_error_redirect()

    user_for_rental = repeat_customer
    if customer_user_id and not is_repeat_submission:
        try:
            user_for_rental = User.objects.get(pk=int(customer_user_id), is_staff=False)
        except (TypeError, ValueError, User.DoesNotExist):
            messages.error(request, "The selected customer account could not be found.")
            return rental_error_redirect()

    if user_for_rental:
        customer_profile, _ = Profile.objects.get_or_create(user=user_for_rental)
        recent_rental = (
            SuitRequest.objects.filter(user=user_for_rental)
            .order_by("-request_date")
            .first()
        )
        first_name = (
            first_name or user_for_rental.first_name
            or (recent_rental.first_name if recent_rental else "")
            or user_for_rental.username
        )
        father_name = (
            father_name or user_for_rental.last_name
            or (recent_rental.second_name if recent_rental else "")
        )
        phone = (
            phone or customer_profile.customer_phone
            or (recent_rental.phone if recent_rental else "")
        )
        national_id = (
            national_id or customer_profile.national_id
            or (recent_rental.national_id if recent_rental else None)
        )
        present_address = (
            present_address or customer_profile.present_address
            or (recent_rental.present_address if recent_rental else "")
        )
        parents_address = (
            parents_address or customer_profile.parents_address
            or (recent_rental.parents_address if recent_rental else "")
        )

    try:
        phone = validate_phone_number(phone)
    except ValueError as e:
        messages.error(request, str(e))
        return rental_error_redirect()

    try:
        national_id = validate_national_id(national_id)
    except ValueError as e:
        messages.error(request, str(e))
        return rental_error_redirect()

    image_file = request.FILES.get("customer_image")
    if image_file:
        try:
            if not is_admin(request.user):
                validate_image_upload(image_file, "customer image")
        except forms.ValidationError as e:
            messages.error(request, str(e))
            return rental_error_redirect()

    try:
        amount_paid = Decimal(amount_paid_raw)
    except (InvalidOperation, TypeError, ValueError):
        messages.error(request, "Please enter a valid amount paid.")
        return rental_error_redirect()

    if not amount_paid.is_finite() or amount_paid < 0:
        messages.error(request, "Amount paid must be a finite, non-negative value.")
        return rental_error_redirect()

    if quantity_requested < 1 or quantity_requested > 5:
        messages.error(request, "Please select a quantity between 1 and 5 suits.")
        return rental_error_redirect()

    if days_requested < 1 or days_requested > 365:
        messages.error(request, "Please select a rental duration between 1 and 365 days.")
        return rental_error_redirect()

    if not start_date or not end_date:
        messages.error(request, "Please provide a valid rental start and end date.")
        return rental_error_redirect()

    if end_date <= start_date:
        messages.error(request, "Return date must be after rental start date.")
        return rental_error_redirect()

    if (end_date - start_date).days != days_requested:
        messages.error(request, "Rental days must match the selected start and return dates.")
        return rental_error_redirect()

    rental_start_time = timezone.make_aware(datetime.combine(start_date, time.min))
    accessory_details = {}
    accessory_prices = {}
    accessory_guarantees = {}

    def read_booking_amount(field_name):
        try:
            value = Decimal(request.POST.get(field_name, "0") or "0")
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"Enter a valid amount for {field_name}.")
        if not value.is_finite() or value < 0:
            raise ValueError("Amounts must be finite and non-negative.")
        return value.quantize(Decimal("0.01"))

    try:
        suit_price_per_day = (
            read_booking_amount("suit_price")
            if suit and "suit_price" in request.POST
            else suit.price_per_day if suit else Decimal("0.00")
        )
        for accessory in accessories:
            submitted_price = read_booking_amount(f"addon_price_{accessory}")
            guarantee = read_booking_amount(f"guarantee_{accessory}")
            if suit and accessory not in ALWAYS_CHARGED:
                price_per_day = Decimal("0.00")
            else:
                price_per_day = submitted_price
                if price_per_day <= 0:
                    raise ValueError(f"Enter a positive daily price for {accessory}.")

            accessory_prices[accessory] = price_per_day
            accessory_guarantees[accessory] = guarantee
            size = request.POST.get(f"{accessory}_size", "").strip()
            color = request.POST.get(f"{accessory}_color", "").strip()
            accessory_details[accessory] = {
                "size": size,
                "color": color,
                "price_per_day": str(price_per_day),
                "guarantee": str(guarantee),
            }

        discount_type = request.POST.get("discount_type", "percent").strip()
        if discount_type not in {"percent", "amount"}:
            raise ValueError("Select a valid discount type.")
        discount_value = read_booking_amount("discount_value")
        calculation = calculate_booking(
            suit_price=suit_price_per_day,
            suit_qty=quantity_requested,
            days=days_requested,
            has_suit=suit is not None,
            prices=accessory_prices,
            guarantees=accessory_guarantees,
            discount_type=discount_type,
            discount_value=discount_value,
        )
        amount_paid = read_booking_amount("amount_paid")
    except ValueError as exc:
        messages.error(request, str(exc))
        return rental_error_redirect()

    if amount_paid > calculation["total_due"]:
        messages.error(request, "Amount paid cannot exceed the total amount to collect.")
        return rental_error_redirect()

    customer_full_name = " ".join(filter(None, [first_name, father_name, grandfather_name])) or "Guest Customer"
    rent_payment = min(amount_paid, calculation["rental_total"])
    guarantee_payment = min(
        max(amount_paid - rent_payment, Decimal("0.00")),
        calculation["guarantee_total"],
    )

    try:
        with transaction.atomic():
            if suit:
                suit = Suit.objects.select_for_update().get(pk=suit.pk)
            if suit and (suit.is_retired or quantity_requested > suit.quantity):
                raise ValidationError(
                    f"Only {suit.quantity} units of {suit.suit_name} are currently available."
                )

            rental = SuitRequest.objects.create(
                suit=suit,
                user=user_for_rental,
                first_name=first_name or None,
                second_name=father_name or None,
                name=customer_full_name,
                phone=phone,
                customer_email=user_for_rental.email if user_for_rental else None,
                national_id=national_id,
                present_address=present_address or None,
                parents_address=parents_address or None,
                notes=notes or None,
                accessory_details=accessory_details,
                suit_price_per_day=suit_price_per_day if suit else None,
                days_requested=days_requested,
                quantity_requested=quantity_requested if suit else 1,
                total_amount=calculation["rental_total"],
                discount_type=discount_type,
                discount_value=discount_value,
                discount_amount=calculation["discount"],
                guarantee_total=calculation["guarantee_total"],
                customer_image=image_file,
                status="Pending",
                is_paid=rent_payment >= calculation["rental_total"],
                payment_method=payment_method.title() if amount_paid > 0 else "",
                payment_reference=payment_reference if amount_paid > 0 else None,
            )
            rental.start_rental(start_time=rental_start_time, performed_by=request.user)

            if user_for_rental:
                updated_user_fields = []
                if first_name and user_for_rental.first_name != first_name and not (
                    not user_for_rental.first_name and first_name == user_for_rental.username
                ):
                    user_for_rental.first_name = first_name
                    updated_user_fields.append("first_name")
                if father_name and user_for_rental.last_name != father_name:
                    user_for_rental.last_name = father_name
                    updated_user_fields.append("last_name")
                if updated_user_fields:
                    user_for_rental.save(update_fields=updated_user_fields)
                customer_profile, _ = Profile.objects.get_or_create(user=user_for_rental)
                updated_profile_fields = []
                profile_values = {
                    "customer_phone": phone,
                    "national_id": national_id,
                    "present_address": present_address,
                    "parents_address": parents_address,
                }
                for field_name, value in profile_values.items():
                    if value and getattr(customer_profile, field_name) != value:
                        setattr(customer_profile, field_name, value)
                        updated_profile_fields.append(field_name)
                if updated_profile_fields:
                    customer_profile.save(update_fields=updated_profile_fields + ["updated_at"])

            if amount_paid > 0:
                payment_status = 'paid' if amount_paid >= calculation["total_due"] else 'partial'
                payment = Payment.objects.create(
                    rental=rental,
                    cashier=request.user,
                    amount_paid=rent_payment,
                    guarantee_amount=guarantee_payment,
                    payment_method=payment_method.title() if payment_method else 'Cash',
                    payment_reference=payment_reference or None,
                    status=payment_status,
                    payment_date=timezone.now(),
                )
                payment.receipt_number = f"RCPT-{payment.id:06d}"
                payment.save(update_fields=['receipt_number'])

                if rent_payment >= calculation["rental_total"] and not rental.is_paid:
                    rental.is_paid = True
                    rental.save(update_fields=["is_paid"])
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return rental_error_redirect()

    messages.success(request, "New rental was created successfully.")
    if repeat_customer:
        return redirect("customer_detail_by_user", user_id=repeat_customer.pk)
    return redirect("requests")


# =========================
# NOTIFICATIONS
# =========================
@login_required
def notifications_view(request):
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread_count = notifications.filter(is_read=False).count()
    return render(request, "admin/notifications.html", {
        "notifications": notifications,
        "unread_count": unread_count,
    })


@login_required
def delete_notification(request, notif_id):
    notification = get_object_or_404(Notification, id=notif_id, user=request.user)
    notification.delete()
    return redirect("notifications")

@login_required
def mark_notification_as_read(request, notif_id):
    notification = get_object_or_404(Notification, id=notif_id, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    return redirect("notifications")

@login_required
def mark_all_notifications_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return redirect("notifications")

@login_required
def receipt_view(request, req_id):
    req = get_object_or_404(SuitRequest, id=req_id)

    # Only staff or the request owner can view the receipt.
    if not is_reception_or_admin(request.user) and req.user != request.user:
        messages.error(request, "You do not have permission to view this receipt.")
        return redirect("requests")

    latest_payment = (
        req.payments.select_related("cashier")
        .order_by("-payment_date")
        .first()
    )
    return render(request, "admin/receipt.html", {
        "req": req,
        "company_name": "Suit Rental",
        "company_address": "123 Main Street, City",
        "company_phone": "+252 61 234 5678",
        "receipt_number": (
            latest_payment.receipt_number
            if latest_payment and latest_payment.receipt_number
            else f"SR-{req.pk:06d}"
        ),
        "reception_name": (
            latest_payment.cashier.get_full_name() or latest_payment.cashier.username
            if latest_payment and latest_payment.cashier
            else "Royal Rental"
        ),
        "total_amount": req.total_amount,
        "total_collected": req.amount_paid + req.guarantee_collected,
        "discount_amount": req.discount_amount,
        "is_admin_user": is_admin(request.user),
        "is_reception_user": is_reception(request.user),
        "hide_admin_sidebar": not is_admin(request.user),
    })


@login_required
def reception_recept_sheet(request, req_id):
    req = get_object_or_404(SuitRequest, id=req_id)

    # Only staff or the request owner can view the reception receipt.
    if not is_reception_or_admin(request.user) and req.user != request.user:
        messages.error(request, "You do not have permission to view this receipt.")
        return redirect("requests")

    receipt_number = f"SR-{req.id:06d}"
    expected_total = (req.rental_subtotal or Decimal('0.00')) + (req.late_fee or Decimal('0.00'))
    total_amount = req.total_amount if req.total_amount is not None else expected_total
    discount_amount = max(expected_total - total_amount, Decimal('0.00'))
    reception_name = request.user.get_full_name() or request.user.username

    return render(request, "reception/reception_recept_sheet.html", {
        "req": req,
        "company_name": "Suit Rental",
        "company_address": "123 Main Street, City",
        "company_phone": "+252 61 234 5678",
        "receipt_number": receipt_number,
        "reception_name": reception_name,
        "discount_amount": discount_amount,
        "total_amount": total_amount,
    })


def create_stripe_checkout_session(req, request):
    stripe_secret = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not stripe_secret:
        raise ValueError("Stripe secret key is not configured.")

    currency = getattr(settings, "STRIPE_CURRENCY", "usd")
    amount_cents = int(req.total_amount * 100)
    session_data = {
        "payment_method_types[]": "card",
        "mode": "payment",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][product_data][name]": (
            f"{req.quantity_requested} x {req.suit.suit_name} rental"
            if req.suit_id
            else "Accessories-only rental"
        ),
        "line_items[0][price_data][product_data][description]": f"{req.days_requested} day rental",
        "line_items[0][price_data][unit_amount]": str(amount_cents),
        "line_items[0][quantity]": "1",
        "success_url": request.build_absolute_uri(reverse("stripe_success", args=[req.id])),
        "cancel_url": request.build_absolute_uri(reverse("stripe_cancel", args=[req.id])),
        "metadata[request_id]": str(req.id),
    }

    request_data = urlencode(session_data).encode()
    stripe_req = UrlRequest("https://api.stripe.com/v1/checkout/sessions", data=request_data, method="POST")
    stripe_req.add_header("Authorization", "Basic " + base64.b64encode((stripe_secret + ":").encode()).decode())
    stripe_req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(stripe_req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        error_body = exc.read().decode()
        raise RuntimeError(error_body or str(exc))


@login_required
def pay_request(request, req_id):
    req = get_object_or_404(SuitRequest, id=req_id)

    if not request.user.is_staff and req.user != request.user:
        messages.error(request, "You do not have permission to pay for this request.")
        return redirect("requests")

    if req.status not in ["Active", "Returned", "Approved"]:
        messages.error(request, "Payment is only available for active, returned, or approved rentals.")
        return redirect("receipt", req.id)

    try:
        session = create_stripe_checkout_session(req, request)
        req.payment_method = "Stripe"
        req.payment_reference = session.get("id")
        req.save(update_fields=["payment_method", "payment_reference"])
        return redirect(session.get("url"))
    except Exception as e:
        messages.error(request, f"Unable to create Stripe checkout session: {e}")
        return redirect("receipt", req.id)


@login_required
def stripe_success(request, req_id):
    req = get_object_or_404(SuitRequest, id=req_id)

    if not request.user.is_staff and req.user != request.user:
        messages.error(request, "You do not have permission to view this payment confirmation.")
        return redirect("requests")

    req.is_paid = True
    req.payment_method = req.payment_method or "Stripe"
    req.save(update_fields=["is_paid", "payment_method"])
    messages.success(request, "Payment completed successfully.")
    return redirect("receipt", req.id)


@login_required
def stripe_cancel(request, req_id):
    req = get_object_or_404(SuitRequest, id=req_id)

    if not request.user.is_staff and req.user != request.user:
        messages.error(request, "You do not have permission to view this payment confirmation.")
        return redirect("requests")

    messages.warning(request, "Stripe payment was canceled.")
    return redirect("receipt", req.id)


# =========================
# REQUESTS PAGE
# =========================
@login_required
def requests_page(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    expire_overdue()

    tab_statuses = {
        "pending": ("Pending",),
        "reserved": ("Approved", "Reserved"),
        "active": ("Active",),
        "returned": ("Returned",),
        "closed": ("Rejected", "Expired"),
    }
    legacy_tab = {
        "Pending": "pending",
        "Approved": "reserved",
        "Reserved": "reserved",
        "Active": "active",
        "Returned": "returned",
        "Rejected": "closed",
        "Expired": "closed",
    }
    requested_tab = request.GET.get("tab", "").strip()
    if not requested_tab:
        requested_tab = legacy_tab.get(request.GET.get("status", ""), "pending")
    active_tab = requested_tab if requested_tab in tab_statuses else "pending"
    query = request.GET.get("q", "").strip()

    all_requests = SuitRequest.objects.select_related("suit")
    if query:
        search_filter = (
            Q(name__icontains=query)
            | Q(phone__icontains=query)
            | Q(national_id__icontains=query)
            | Q(suit__suit_name__icontains=query)
        )
        numeric_query = query.lstrip("#")
        if numeric_query.isdigit() and len(numeric_query) <= 18:
            search_filter |= Q(pk=int(numeric_query))
        all_requests = all_requests.filter(search_filter)

    counts = {
        "pending": all_requests.filter(status="Pending").count(),
        "reserved": all_requests.filter(status__in=tab_statuses["reserved"]).count(),
        "active": all_requests.filter(status="Active").count(),
        "returned": all_requests.filter(status="Returned").count(),
        "closed": all_requests.filter(status__in=tab_statuses["closed"]).count(),
    }
    requests = all_requests.filter(status__in=tab_statuses[active_tab])
    requests = requests.order_by("waiting_deadline", "-request_date") if active_tab == "reserved" else requests.order_by("-request_date")
    page_obj = Paginator(requests, 10).get_page(request.GET.get("page"))

    available_count = _available_inventory_queryset().count()
    reserved_count = Suit.objects.filter(status="Reserved").count()
    rented_count = Suit.objects.filter(status="Rented").count()
    returned_count = Suit.objects.filter(status="Returned").count()
    dry_cleaning_count = Suit.objects.filter(status="Dry Cleaning").count()

    context = {
        "requests": page_obj.object_list,
        "page_obj": page_obj,
        "tabs": [
            {"key": "pending", "label": "Pending", "icon": "fa-hourglass-half", "count": counts["pending"]},
            {"key": "reserved", "label": "Reserved", "icon": "fa-bookmark", "count": counts["reserved"]},
            {"key": "active", "label": "Active", "icon": "fa-shirt", "count": counts["active"]},
            {"key": "returned", "label": "Returned", "icon": "fa-rotate-left", "count": counts["returned"]},
            {"key": "closed", "label": "Rejected / Expired", "icon": "fa-ban", "count": counts["closed"]},
        ],
        "active_tab": active_tab,
        "current_q": query,
        "active_requests": SuitRequest.objects.filter(status="Active").select_related("suit").order_by("-request_date"),
        "late_fee_policy": RentalPolicy.get_solo(),
        "return_payment_key": str(uuid4()),
        "total_count": all_requests.count(),
        "pending_count": counts["pending"],
        "approved_count": SuitRequest.objects.filter(status="Approved").count(),
        "rejected_count": SuitRequest.objects.filter(status="Rejected").count(),
        "active_count": counts["active"],
        "returned_count": returned_count,
        "expired_count": SuitRequest.objects.filter(status="Expired").count(),
        "current_status": request.GET.get("status", ""),
        "available_count": available_count,
        "reserved_count": reserved_count,
        "rented_count": rented_count,
        "returned_suit_count": returned_count,
        "dry_cleaning_count": dry_cleaning_count,
    }

    return render(request, "admin/admin_requests.html", context)


@login_required
def customers(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    customers = SuitRequest.objects.values("name", "phone").annotate(
        total_rentals=Count("id"),
        active_rentals=Count("id", filter=Q(status="Active")),
        returned_rentals=Count("id", filter=Q(status="Returned")),
        pending_rentals=Count("id", filter=Q(status="Pending")),
    ).order_by("-total_rentals", "name")

    context = {
        "customers": customers,
        "customer_count": customers.count(),
        "total_rentals": SuitRequest.objects.count(),
    }
    return render(request, "customer/customers.html", context)


@login_required
def reception_requests(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_ready":
            request_id = request.POST.get("request_id")
            suit_id = request.POST.get("suit_id")
            cleaning_cost = request.POST.get("cleaning_cost", "")
            laundry_notes = request.POST.get("laundry_notes", "")

            if not request_id:
                messages.error(request, "No rental request selected.")
                return redirect("requests")

            suit_request = SuitRequest.objects.filter(id=request_id, status="Returned").select_related("suit").first()
            if not suit_request:
                messages.error(request, "That rental request is not available for this action.")
                return redirect("requests")

            dry_clean = DryCleaning.objects.filter(
                rental_request=suit_request,
                status__in=["completed", "approved"],
            ).order_by("-created_at").first()
            if not dry_clean:
                dry_clean = DryCleaning.objects.filter(
                    suit=suit_request.suit,
                    rental_request__isnull=True,
                    status="approved",
                ).order_by("-created_at").first()
            if not dry_clean:
                messages.error(request, f"Laundry must be completed before '{suit_request.suit.suit_name}' can return to inventory.")
                return redirect("requests")

            with transaction.atomic():
                suit = Suit.objects.select_for_update().get(pk=suit_request.suit_id)
                dry_clean = DryCleaning.objects.select_for_update().get(pk=dry_clean.pk)
                if dry_clean.status not in ["completed", "approved"]:
                    messages.error(request, "This cleaning job has already been returned to inventory.")
                    return redirect("requests")

                try:
                    amount = Decimal(cleaning_cost) if cleaning_cost not in [None, ""] else dry_clean.amount
                except (InvalidOperation, TypeError, ValueError):
                    amount = dry_clean.amount

                expense_exists = Expense.objects.filter(
                    description__icontains=f"Dry cleaning for {suit_request.suit.suit_name}"
                ).exists()

                if not expense_exists:
                    Expense.objects.create(
                        description=f"Dry cleaning for {suit_request.suit.suit_name} (Tag: #{suit_request.suit.id}). {laundry_notes}",
                        amount=amount,
                        created_by=request.user,
                        notes=laundry_notes or f"Dry cleaning cost for {suit_request.suit.suit_name}",
                    )

                quantity_delta = suit_request.quantity_requested
                suit.quantity += suit_request.quantity_requested
                suit.status = "Available"
                suit.save(update_fields=["quantity", "status"])
                record_inventory_movement(
                    suit,
                    "cleaning_completed",
                    quantity_delta=quantity_delta,
                    performed_by=request.user,
                    rental_request=suit_request,
                    notes="Cleaned items returned to available inventory.",
                )

                suit_request.status = "Returned"
                suit_request.save(update_fields=["status"])

                dry_clean.paid = True
                dry_clean.status = "returned"
                dry_clean.returned_at = timezone.now()
                dry_clean.save(update_fields=["paid", "status", "returned_at"])

                messages.success(request, f"{suit.suit_name} is now ready and removed from dry cleaning.")

            return redirect("requests")

    # --- Filters from GET params ---
    search_query = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', 'all').strip().lower()
    date_filter = request.GET.get('date', '').strip()
    month_filter = request.GET.get('month', '').strip()  # format: YYYY-MM
    year_filter = request.GET.get('year', '').strip()
    page = request.GET.get("page", 1)

    requests_qs = SuitRequest.objects.select_related('suit').prefetch_related('suit__dry_cleanings').order_by('-request_date')

    # Text search
    if search_query:
        requests_qs = requests_qs.filter(
            Q(name__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(suit__suit_name__icontains=search_query) |
            Q(national_id__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(second_name__icontains=search_query)
        )

    # Status filter
    status_map = {
        'pending': 'Pending',
        'active': 'Active',
        'returned': 'Returned',
        'rejected': 'Rejected',
        'approved': 'Approved',
        'reserved': 'Reserved',
        'expired': 'Expired',
    }
    if status_filter == 'reserved':
        requests_qs = requests_qs.filter(status__in=['Approved', 'Reserved'])
    elif status_filter == 'dry-cleaning':
        requests_qs = requests_qs.filter(suit__status='Dry Cleaning')
    elif status_filter == 'available':
        requests_qs = requests_qs.filter(suit__status='Available')
    elif status_filter != 'all' and status_filter in status_map:
        requests_qs = requests_qs.filter(status=status_map[status_filter])

    # Date filter (specific date)
    if date_filter:
        parsed_date, date_error = parse_historical_date(date_filter)
        if date_error:
            messages.error(request, date_error)
        if parsed_date:
            requests_qs = requests_qs.filter(request_date__date=parsed_date)

    # Month filter (YYYY-MM), only if no date filter
    if month_filter and not date_filter:
        try:
            year_m, month_m = month_filter.split('-')
            requests_qs = requests_qs.filter(
                request_date__year=int(year_m),
                request_date__month=int(month_m)
            )
        except (ValueError, AttributeError):
            pass
    elif year_filter and not date_filter and not month_filter:
        # Year only
        try:
            requests_qs = requests_qs.filter(request_date__year=int(year_filter))
        except ValueError:
            pass

    # Stats (computed after filters so they reflect filtered set)
    pending_count = requests_qs.filter(status='Pending').count()
    active_count = requests_qs.filter(status='Active').count()
    reserved_count = requests_qs.filter(status__in=['Reserved', 'Approved']).count()
    rejected_count = requests_qs.filter(status='Rejected').count()
    total_count = requests_qs.count()

    recent_rentals = SuitRequest.objects.filter(status="Returned").select_related('suit').order_by("-return_date")[:10]

    available_count = _available_inventory_queryset().count()
    rented_count = Suit.objects.filter(status="Rented").count()
    returned_count = Suit.objects.filter(status="Returned").count()
    dry_cleaning_count = Suit.objects.filter(status="Dry Cleaning").count()

    # Pagination
    paginator = Paginator(requests_qs, 20)
    page_obj = paginator.get_page(page)

    # Available years for year dropdown
    available_years = list(
        SuitRequest.objects.values_list('request_date__year', flat=True)
        .distinct().order_by('-request_date__year')
    )

    context = {
        'requests': page_obj,
        'page_obj': page_obj,
        'recent_rentals': recent_rentals,
        'active_requests': SuitRequest.objects.filter(status="Active").select_related('suit'),
        'late_fee_policy': RentalPolicy.get_solo(),
        'return_payment_key': str(uuid4()),
        'total_count': total_count,
        'pending_count': pending_count,
        'approved_count': SuitRequest.objects.filter(status="Approved").count(),
        'active_count': active_count,
        'rejected_count': rejected_count,
        'returned_count': SuitRequest.objects.filter(status="Returned").count(),
        'expired_count': SuitRequest.objects.filter(status="Expired").count(),
        'current_status': status_filter,
        'search_query': search_query,
        'date_filter': date_filter,
        'month_filter': month_filter,
        'year_filter': year_filter,
        'available_years': available_years,
        'available_count': available_count,
        'reserved_count': reserved_count,
        'rented_count': rented_count,
        'returned_suit_count': returned_count,
        'dry_cleaning_count': dry_cleaning_count,
    }
    return render(request, "reception/reception_requests.html", context)


@login_required
def customer_search(request):
    if not is_reception_or_admin(request.user):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse([], safe=False)

    customers = SuitRequest.objects.filter(
        Q(name__icontains=query) | Q(phone__icontains=query)
    ).values('name', 'phone').distinct()[:10]

    return JsonResponse(list(customers), safe=False)


# =========================
# CUSTOMER DETAIL
# =========================
@login_required
def customer_detail(request, customer_name):
    if request.user.is_authenticated and not request.user.is_staff:
        context = _customer_history_context(request.user)
        context["hide_admin_sidebar"] = True
        context["can_manage_customer_rentals"] = False
        return render(request, "admin/customer_detail.html", context)
    elif not is_reception_or_cashier_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Cashier/Admin only.")
        return redirect("home")
    else:
        customer_requests = SuitRequest.objects.filter(name=customer_name).order_by("-request_date")

    if not customer_requests.exists():
        if request.user.is_authenticated and not request.user.is_staff:
            messages.error(request, "You have no rental history yet.")
            return redirect("customer_history")
        messages.error(request, "Customer not found.")
        return redirect("requests")
    
    # Get first request to grab customer details
    first_req = customer_requests.first()
    if first_req.user_id:
        context = _customer_history_context(first_req.user)
        context["is_reception_user"] = is_reception(request.user)
        context["hide_admin_sidebar"] = context["is_reception_user"]
        context["can_manage_customer_rentals"] = _can_manage_customer_rentals(request.user)
        return render(request, "admin/customer_detail.html", context)

    customer_profile = None
    customer_requests = list(
        customer_requests.select_related("suit").prefetch_related("payments")
    )
    for rental in customer_requests:
        rental.history_payments = list(rental.payments.all())
        rental.history_amount_paid = sum(
            (payment.amount_paid for payment in rental.history_payments),
            Decimal("0.00"),
        )
        rental.history_balance_due = max(
            rental.total_amount - rental.history_amount_paid,
            Decimal("0.00"),
        )
    
    # Calculate stats
    total_rentals = len(customer_requests)
    active_rentals = sum(rental.status == "Active" for rental in customer_requests)
    returned_rentals = sum(rental.status == "Returned" for rental in customer_requests)
    pending_rentals = sum(rental.status == "Pending" for rental in customer_requests)
    total_spent = sum(
        (
            rental.total_amount
            for rental in customer_requests
            if rental.status in {"Active", "Returned"}
        ),
        Decimal("0.00"),
    )
    
    context = {
        'customer_name': first_req.name,
        'customer_phone': first_req.phone,
        'customer_id': first_req.national_id,
        'customer_image': first_req.customer_image,
        'customer_profile': customer_profile,
        'customer_present_address': first_req.present_address,
        'customer_parents_address': first_req.parents_address,
        'requests': customer_requests,
        'latest_rental': customer_requests[0] if customer_requests else None,
        'total_rentals': total_rentals,
        'active_rentals': active_rentals,
        'active_rental_records': [rental for rental in customer_requests if rental.status == "Active"],
        'overdue_rentals': sum(rental.is_overdue for rental in customer_requests),
        'returned_rentals': returned_rentals,
        'pending_rentals': pending_rentals,
        'total_spent': total_spent,
        'total_paid': sum(
            (rental.history_amount_paid for rental in customer_requests),
            Decimal("0.00"),
        ),
        'hide_admin_sidebar': False,
        'can_manage_customer_rentals': is_admin(request.user),
        'total_balance_due': sum(
            (rental.history_balance_due for rental in customer_requests),
            Decimal("0.00"),
        ),
    }
    return render(request, "admin/customer_detail.html", context)


@login_required
def customer_detail_by_user(request, user_id):
    if not _can_manage_customer_rentals(request.user):
        messages.error(request, "Access denied. Admin or Reception only.")
        return redirect("home")

    customer_user = _get_rental_customer_or_404(user_id)
    context = _customer_history_context(customer_user)
    context["is_reception_user"] = is_reception(request.user)
    context["hide_admin_sidebar"] = context["is_reception_user"]
    context["can_manage_customer_rentals"] = True
    return render(request, "admin/customer_detail.html", context)


def _customer_history_context(customer_user):
    customer_profile = Profile.objects.filter(user=customer_user).first() or Profile(
        user=customer_user
    )
    customer_requests = list(
        SuitRequest.objects.filter(user=customer_user)
        .select_related("suit")
        .prefetch_related("payments")
        .order_by("-request_date")
    )

    for rental in customer_requests:
        rental.history_payments = list(rental.payments.all())
        rental.history_amount_paid = sum(
            (payment.amount_paid for payment in rental.history_payments),
            Decimal("0.00"),
        )
        rental.history_balance_due = max(
            rental.total_amount - rental.history_amount_paid,
            Decimal("0.00"),
        )

    latest_rental = customer_requests[0] if customer_requests else None
    active_rental_records = [rental for rental in customer_requests if rental.status == "Active"]
    return {
        "customer_name": (
            latest_rental.name if latest_rental else ""
        ) or customer_user.get_full_name() or customer_user.username,
        "customer_phone": (
            customer_profile.customer_phone
            or (latest_rental.phone if latest_rental else "")
        ),
        "customer_id": (
            customer_profile.national_id
            or (latest_rental.national_id if latest_rental else "")
            or customer_user.username
        ),
        "customer_present_address": (
            customer_profile.present_address
            or (latest_rental.present_address if latest_rental else "")
        ),
        "customer_parents_address": (
            customer_profile.parents_address
            or (latest_rental.parents_address if latest_rental else "")
        ),
        "customer_image": (
            latest_rental.customer_image
            if latest_rental and latest_rental.customer_image
            else customer_profile.profile_photo
        ),
        "customer_profile": customer_profile,
        "customer_user": customer_user,
        "requests": customer_requests,
        "latest_rental": latest_rental,
        "active_rental_records": active_rental_records,
        "total_rentals": len(customer_requests),
        "active_rentals": len(active_rental_records),
        "overdue_rentals": sum(rental.is_overdue for rental in active_rental_records),
        "returned_rentals": sum(rental.status == "Returned" for rental in customer_requests),
        "pending_rentals": sum(rental.status == "Pending" for rental in customer_requests),
        "total_spent": sum(
            (
                rental.total_amount
                for rental in customer_requests
                if rental.status in {"Active", "Returned"}
            ),
            Decimal("0.00"),
        ),
        "total_paid": sum(
            (rental.history_amount_paid for rental in customer_requests),
            Decimal("0.00"),
        ),
        "total_balance_due": sum(
            (rental.history_balance_due for rental in customer_requests),
            Decimal("0.00"),
        ),
    }


@login_required
def new_rent_for_customer(request, user_id):
    if not _can_manage_customer_rentals(request.user):
        messages.error(request, "Access denied. Admin or Reception only.")
        return redirect("home")

    customer_user = _get_rental_customer_or_404(user_id)
    customer_profile = Profile.objects.filter(user=customer_user).first() or Profile(
        user=customer_user
    )
    recent_request = (
        SuitRequest.objects.filter(user=customer_user)
        .order_by("-request_date")
        .first()
    )
    prefill = {
        "customer_id": customer_user.pk,
        "first_name": (
            customer_user.first_name
            or (recent_request.first_name if recent_request else "")
            or customer_user.username
        ),
        "father_name": (
            customer_user.last_name
            or (recent_request.second_name if recent_request else "")
            or ""
        ),
        "grandfather_name": "",
        "phone": customer_profile.customer_phone or (recent_request.phone if recent_request else ""),
        "present_address": (
            customer_profile.present_address
            or (recent_request.present_address if recent_request else "")
        ),
        "national_id": (
            customer_profile.national_id
            or (recent_request.national_id if recent_request else "")
        ),
        "parents_address": (
            customer_profile.parents_address
            or (recent_request.parents_address if recent_request else "")
        ),
    }
    context = _customer_history_context(customer_user)
    available_suits = list(_available_inventory_queryset().order_by("suit_name"))
    for suit in available_suits:
        suit.booking_available_quantity = suit.quantity - suit._held_units

    context.update({
        "is_reception_user": is_reception(request.user),
        "hide_admin_sidebar": is_reception(request.user),
        "can_manage_customer_rentals": True,
        "show_new_rent_form": True,
        "available_suits": available_suits,
        "rental_day_options": [(days, days * 24) for days in range(1, 31)],
        "prefill_customer_id": prefill["customer_id"],
        "prefill_first_name": prefill["first_name"],
        "prefill_father_name": prefill["father_name"],
        "prefill_grandfather_name": prefill["grandfather_name"],
        "prefill_phone": prefill["phone"],
        "prefill_present_address": prefill["present_address"],
        "prefill_national_id": prefill["national_id"],
        "prefill_parents_address": prefill["parents_address"],
        "rental_form_values": request.session.pop(
            f"repeat_rental_form_values_{customer_user.pk}", {}
        ),
        "default_start_date": timezone.localdate().isoformat(),
        "default_end_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
    })
    token_key = f"repeat_rental_token_{customer_user.pk}"
    context["rental_submission_token"] = uuid4().hex
    request.session[token_key] = context["rental_submission_token"]
    return render(request, "admin/customer_detail.html", context)


@login_required
def send_return_reminder(request, req_id):
    """Send email reminder to customer to return rented suit"""
    if not request.user.is_staff:
        messages.error(request, "Access denied. Admin/Cashier only.")
        return redirect("home")
    
    req = get_object_or_404(SuitRequest, id=req_id)
    
    if req.status != "Active":
        messages.error(request, "Can only send reminders for active rentals.")
        return redirect('customer_detail', customer_name=req.name)
    
    if request.method == 'POST':
        # Update email if provided
        email = request.POST.get('customer_email', '').strip()
        if email:
            req.customer_email = email
            req.save(update_fields=['customer_email'])
        
        # Send email
        sent, info = send_return_reminder_email(req)
        
        if sent:
            req.is_email_sent = True
            req.save(update_fields=['is_email_sent'])
            Notification.objects.create(
                user=request.user, 
                message=f"Email reminder sent to {req.name} for {req.suit.suit_name if req.suit_id else 'accessories'}"
            )
            messages.success(request, f"Return reminder email sent to {req.name}.")
        else:
            messages.error(request, f"Failed to send email: {info}")
        
        return redirect('customer_detail', customer_name=req.name)
    
    # GET: Show form to edit email before sending
    return render(request, 'admin/send_email_reminder.html', {
        'req': req,
        'suit': req.suit,
    })


@login_required
def reception_send_sms(request, req_id):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect('dashboard')

    req = get_object_or_404(SuitRequest, id=req_id)

    if request.method == 'POST':
        message_text = request.POST.get('message', '').strip()
        if not message_text:
            messages.error(request, 'Message cannot be empty.')
            return redirect('send_return_reminder', req_id=req_id)

        # Send SMS via sms utility
        try:
            sent, info = send_sms(req.phone, message_text)
            Notification.objects.create(user=request.user, message=f"SMS to {req.name}: {message_text}")
            if sent:
                messages.success(request, f"SMS sent to {req.name} ({req.phone}).")
                # Optionally mark the request as notified
                req.is_notified = True
                req.save(update_fields=['is_notified'])
            else:
                # Provide a helpful hint depending on info
                if info == 'no-config':
                    messages.warning(request, 'SMS not sent: SMS not configured. Set SMS_API_URL or SMS_BACKEND in settings.')
                else:
                    messages.warning(request, f'SMS not sent ({info}).')
        except Exception as e:
            messages.error(request, f'Error sending SMS: {e}')

        return redirect('customer_detail', customer_name=req.name)

    # GET: show form
    item_name = req.suit.suit_name if req.suit_id else "agabka"
    default_message = f"Salaan {req.name}, xusuusin: wakhtiga kirada ee '{item_name}' ayaa la joogo. Fadlan soo celi ama la xiriir {request.user.username}."
    return render(request, 'reception/reception_send_sms.html', {'req': req, 'default_message': default_message})


# =========================
# APPROVE REQUEST
# =========================
@login_required
def approve_request(request, req_id):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    req = get_object_or_404(SuitRequest, id=req_id)
    return_url = "requests" if is_admin(request.user) else "requests"

    pickup_mode_default = False
    action_mode_default = 'approve'
    if request.method == "GET":
        pickup_mode_default = request.GET.get('pickup') == '1' or request.GET.get('mode') == 'pickup'
        if pickup_mode_default or req.status in {'Reserved', 'Approved'}:
            action_mode_default = 'rent_now'
    
    if req.status not in {"Pending", "Approved", "Reserved"}:
        messages.error(request, "Only pending, approved, or reserved requests can be processed.")
        return redirect(return_url)

    if request.method == "POST":
        amount_paid_raw = request.POST.get("amount_paid", "0").strip() or "0"
        payment_method = request.POST.get("payment_method", "cash").strip()
        payment_reference = request.POST.get("payment_reference", "").strip()
        accessories = list(dict.fromkeys(request.POST.getlist("accessories")))
        if (
            "accessories_configured" not in request.POST
            and not accessories
            and req.status in {"Approved", "Reserved"}
        ):
            stored_accessories = req.accessory_items
            accessories = [
                item
                for item in ACCESSORY_TYPES
                if any(detail.lower().startswith(item) for detail in stored_accessories)
            ]
        if any(accessory not in ACCESSORY_TYPES for accessory in accessories):
            messages.error(request, "Please select valid accessories.")
            return redirect("approve_request", req.id)
        action_mode = request.POST.get("action_mode", "approve").strip()
        id_document_type = request.POST.get("id_document_type", "National ID").strip()
        id_document_number = request.POST.get("id_document_number", "").strip()
        reserve_duration_raw = request.POST.get("reserve_duration", "24").strip()
        reserve_custom_hours_raw = request.POST.get("reserve_custom_hours", "").strip()

        if action_mode not in {"approve", "rent_now", "reserve"}:
            messages.error(request, "Choose approval, immediate issue, or reservation.")
            return redirect("approve_request", req.id)

        try:
            amount_paid = Decimal(amount_paid_raw)
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, "Please enter a valid payment amount.")
            return redirect("approve_request", req.id)

        try:
            accessory_prices = {}
            accessory_guarantees = {}
            accessory_details_data = {}
            for accessory in accessories:
                price = (
                    Decimal(request.POST.get(
                        f"addon_price_{accessory}",
                        str(ACCESSORY_DAILY_PRICE),
                    ) or "0")
                    if not req.suit_id or accessory in ALWAYS_CHARGED
                    else Decimal("0.00")
                )
                guarantee = Decimal(request.POST.get(f"guarantee_{accessory}", "0") or "0")
                if (
                    not price.is_finite()
                    or price < 0
                    or not guarantee.is_finite()
                    or guarantee < 0
                ):
                    raise ValueError("Accessory prices and guarantees must be non-negative amounts.")
                accessory_prices[accessory] = price
                accessory_guarantees[accessory] = guarantee
                accessory_details_data[accessory] = {
                    "size": request.POST.get(f"{accessory}_size", "").strip(),
                    "color": request.POST.get(f"{accessory}_color", "").strip(),
                    "price_per_day": str(price.quantize(Decimal("0.01"))),
                    "guarantee": str(guarantee.quantize(Decimal("0.01"))),
                }

            discount_type = request.POST.get("discount_type", "percent").strip()
            discount_value = Decimal(request.POST.get("discount_value", "0") or "0")
            if not discount_value.is_finite() or discount_value < 0:
                raise ValueError("Discount must be a non-negative amount.")
            calculation = calculate_booking(
                suit_price=req.booked_suit_price,
                suit_qty=req.quantity_requested or 1,
                days=req.days_requested or 1,
                has_suit=bool(req.suit_id),
                prices=accessory_prices,
                guarantees=accessory_guarantees,
                discount_type=discount_type,
                discount_value=discount_value,
            )
            if not amount_paid.is_finite() or amount_paid < 0:
                raise ValueError("Amount paid must be a finite, non-negative amount.")
        except (InvalidOperation, TypeError, ValueError) as exc:
            messages.error(request, str(exc) or "Enter valid booking amounts.")
            return redirect("approve_request", req.id)

        total_amount = calculation["rental_total"]
        guarantee_total = calculation["guarantee_total"]
        existing_guarantee = req.guarantee_collected
        if guarantee_total < existing_guarantee:
            messages.error(request, "The guarantee cannot be reduced below the amount already collected.")
            return redirect("approve_request", req.id)
        current_balance = max(total_amount - req.amount_paid, Decimal("0.00"))
        guarantee_balance = max(guarantee_total - existing_guarantee, Decimal("0.00"))
        total_balance = current_balance + guarantee_balance
        if amount_paid > total_balance:
            messages.error(request, "Amount collected cannot exceed the remaining rent and guarantee.")
            return redirect("approve_request", req.id)
        rent_payment = min(amount_paid, current_balance)
        guarantee_payment = min(
            max(amount_paid - rent_payment, Decimal("0.00")),
            guarantee_balance,
        )

        reserve_hours = None
        if reserve_custom_hours_raw:
            try:
                reserve_hours = int(reserve_custom_hours_raw)
            except (TypeError, ValueError):
                reserve_hours = None
        if reserve_hours is None:
            try:
                reserve_hours = int(reserve_duration_raw)
            except (TypeError, ValueError):
                reserve_hours = 24
        if reserve_hours <= 0:
            reserve_hours = 24

        notes = "\n".join(
            line for line in (req.notes or "").splitlines()
            if not line.strip().startswith("Accessories:")
        )
        req.payment_method = payment_method.title() if payment_method else 'Cash'
        req.payment_reference = payment_reference or None
        if id_document_number:
            if notes:
                notes += "\n"
            notes += f"{id_document_type}: {id_document_number}"
            if id_document_type == "National ID":
                req.national_id = id_document_number

        accessory_notes = []
        for accessory in ACCESSORY_TYPES:
            if accessory not in accessories:
                continue
            existing_detail = next(
                (
                    detail for detail in req.accessory_items
                    if detail.lower().startswith(accessory)
                ),
                None,
            )
            size = request.POST.get(f"{accessory}_size", "").strip()
            color = request.POST.get(f"{accessory}_color", "").strip()
            if existing_detail and not size:
                accessory_notes.append(existing_detail)
            else:
                accessory_notes.append(f"{accessory} ({size} / {color})")

        if accessory_notes:
            if notes:
                notes += "\n"
            notes += f"Accessories: {', '.join(accessory_notes)}"

        def apply_booking_fields(target):
            target.accessory_details = accessory_details_data
            target.suit_price_per_day = (
                target.suit_price_per_day
                if target.suit_price_per_day is not None
                else target.suit.price_per_day if target.suit_id else None
            )
            target.discount_type = discount_type
            target.discount_value = discount_value
            target.discount_amount = calculation["discount"]
            target.guarantee_total = guarantee_total

        if action_mode == "reserve":
            reservation_expires_at = timezone.now() + timedelta(hours=reserve_hours)
            try:
                if not req.reserve_inventory(reservation_expires_at, performed_by=request.user):
                    messages.error(request, "This request already has an active inventory reservation.")
                    return redirect(return_url)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect(return_url)

            req.status = "Reserved"
            req.rent_days = req.days_requested or req.rent_days or 1
            req.payment_method = payment_method.title() if payment_method else 'Cash'
            req.payment_reference = payment_reference or None
            if not req.approved_at:
                req.approved_at = timezone.now()
            req.due_date = reservation_expires_at
            if notes:
                notes += "\n"
            notes += f"Reserved pickup for {reserve_hours} hours"
            req.notes = notes.strip()
            req.total_amount = total_amount
            apply_booking_fields(req)
            req.save(update_fields=[
                'status', 'approved_at', 'due_date', 'rental_start_time',
                'rent_days', 'total_amount', 'notes', 'national_id',
                'payment_method', 'payment_reference', 'accessory_details',
                'suit_price_per_day',
                'discount_type', 'discount_value', 'discount_amount',
                'guarantee_total',
            ])

            if amount_paid > 0:
                payment_status = 'paid' if amount_paid >= total_balance else 'partial'
                payment = Payment.objects.create(
                    rental=req,
                    cashier=request.user,
                    amount_paid=rent_payment,
                    guarantee_amount=guarantee_payment,
                    payment_method=payment_method.title() if payment_method else 'Cash',
                    payment_reference=payment_reference or None,
                    status=payment_status,
                    payment_date=timezone.now(),
                )
                payment.receipt_number = f"RCPT-{payment.id:06d}"
                payment.save(update_fields=['receipt_number'])

                if rent_payment >= current_balance:
                    req.is_paid = True
                    req.save(update_fields=['is_paid'])

            messages.success(request, "Request reserved for pickup successfully.")
            return redirect(return_url)

        if action_mode == "approve":
            if req.status == "Reserved":
                messages.error(request, "A reserved request must be issued at pickup or kept reserved.")
                return redirect(return_url)

            try:
                with transaction.atomic():
                    approved_request = SuitRequest.objects.select_for_update().get(pk=req.pk)
                    remaining_balance = max(total_amount - approved_request.amount_paid, Decimal("0.00"))
                    if amount_paid > remaining_balance:
                        raise ValidationError("Amount paid cannot exceed the remaining rental balance.")

                    approved_request.status = "Approved"
                    approved_request.approved_at = approved_request.approved_at or timezone.now()
                    approved_request.rent_days = approved_request.days_requested or approved_request.rent_days or 1
                    approved_request.total_amount = total_amount
                    approved_request.notes = notes.strip()
                    approved_request.payment_method = payment_method.title() if payment_method else "Cash"
                    approved_request.payment_reference = payment_reference or None
                    apply_booking_fields(approved_request)
                    if id_document_number and id_document_type == "National ID":
                        approved_request.national_id = id_document_number
                    approved_request.save(update_fields=[
                        "status", "approved_at", "rent_days", "total_amount", "notes",
                        "payment_method", "payment_reference", "national_id",
                        "accessory_details", "discount_type", "discount_value",
                        "discount_amount", "guarantee_total", "suit_price_per_day",
                    ])

                    if amount_paid > 0:
                        payment = Payment.objects.create(
                            rental=approved_request,
                            cashier=request.user,
                            amount_paid=rent_payment,
                            guarantee_amount=guarantee_payment,
                            payment_method=payment_method.title() if payment_method else "Cash",
                            payment_reference=payment_reference or None,
                            status="paid" if amount_paid >= total_balance else "partial",
                            payment_date=timezone.now(),
                        )
                        payment.receipt_number = f"RCPT-{payment.id:06d}"
                        payment.save(update_fields=["receipt_number"])
                        if rent_payment >= current_balance:
                            approved_request.is_paid = True
                            approved_request.save(update_fields=["is_paid"])
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect("approve_request", req.id)

            messages.success(request, "Request approved. The rental timer starts only when the item is issued.")
            return redirect(return_url)

        try:
            with transaction.atomic():
                issue_request = SuitRequest.objects.select_for_update().get(pk=req.pk)
                issue_request.notes = notes.strip()
                issue_request.total_amount = total_amount
                apply_booking_fields(issue_request)
                if id_document_number and id_document_type == "National ID":
                    issue_request.national_id = id_document_number
                issue_request.payment_method = payment_method.title() if payment_method else "Cash"
                issue_request.payment_reference = payment_reference or None
                issue_request.save(update_fields=[
                    "notes", "total_amount", "national_id",
                    "payment_method", "payment_reference",
                    "accessory_details", "discount_type", "discount_value",
                    "discount_amount", "guarantee_total", "suit_price_per_day",
                ])

                if not issue_request.start_rental(
                    start_time=timezone.now(),
                    performed_by=request.user,
                ):
                    raise ValidationError("This rental has already been issued.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(return_url)

        if amount_paid > 0:
            payment_status = 'paid' if amount_paid >= total_balance else 'partial'
            payment = Payment.objects.create(
                rental=req,
                cashier=request.user,
                amount_paid=rent_payment,
                guarantee_amount=guarantee_payment,
                payment_method=payment_method.title() if payment_method else 'Cash',
                payment_reference=payment_reference or None,
                status=payment_status,
                payment_date=timezone.now(),
            )
            payment.receipt_number = f"RCPT-{payment.id:06d}"
            payment.save(update_fields=['receipt_number'])

            if rent_payment >= current_balance:
                req.is_paid = True
                req.save(update_fields=['is_paid'])

        messages.success(request, "Rental issued to customer. The rental timer has started.")
        return redirect(return_url)

    total_amount = req.booked_suit_price * req.days_requested * req.quantity_requested
    if not req.total_amount:
        req.total_amount = total_amount
        req.save(update_fields=['total_amount'])

    existing_paid = req.amount_paid
    existing_balance = req.balance_due
    context = {
        'req': req,
        'total_amount': total_amount,
        'existing_paid': existing_paid,
        'existing_balance': existing_balance,
        'back_url': return_url,
        'action_mode_default': action_mode_default,
        'pickup_mode_default': pickup_mode_default,
        'accessory_daily_price': ACCESSORY_DAILY_PRICE,
        'accessory_types': ACCESSORY_TYPES,
        'accessory_guarantee_fields': [
            {
                "accessory": accessory,
                "value": (req.accessory_details.get(accessory) or {}).get("guarantee", "0.00"),
            }
            for accessory in ACCESSORY_TYPES
        ],
        'issue_date': timezone.localdate(),
        'selected_accessories': [
            accessory
            for accessory in ACCESSORY_TYPES
            if any(
                detail.lower().startswith(accessory)
                for detail in req.accessory_items
            )
        ],
    }
    template_name = "admin/admin_approve.html" if is_admin(request.user) else "reception/reception_approve.html"
    return render(request, template_name, context)


# =========================
# REJECT REQUEST
# =========================
@login_required
@require_POST
@transaction.atomic
def reject_request(request, req_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    req = get_object_or_404(SuitRequest, id=req_id)
    reason = request.POST.get("reason", "").strip()[:255]
    if req.status not in {"Pending", "Approved", "Reserved"}:
        messages.error(request, f"Request #{req.pk} cannot be rejected in its current status.")
        return redirect(f"{reverse('requests')}?tab=closed")

    try:
        change_status(
            req,
            "cancelled",
            user=request.user,
            note=reason or "Request rejected by administrator.",
        )
    except WorkflowError as exc:
        messages.error(request, str(exc))
        return redirect("requests")

    req.rejected_reason = reason
    req.save(update_fields=["rejected_reason"])
    messages.success(request, f"Request #{req.pk} was rejected.")
    return redirect(f"{reverse('requests')}?tab=closed")


# =========================
# RETURN SUIT
# =========================
@login_required
@transaction.atomic
def return_suit(request, req_id):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    req = get_object_or_404(
        SuitRequest.objects.select_for_update().select_related("suit"),
        id=req_id,
    )

    if req.guarantee_collected and not req.guarantee_settled_at:
        messages.error(request, "Use the detailed return form to settle the accessory guarantee.")
        return redirect("requests")

    if req.status == "Active":
        return_condition = request.POST.get("return_condition", "good") if request.method == "POST" else "good"
        if return_condition not in dict(SuitRequest.RETURN_CONDITION_CHOICES):
            messages.error(request, "Select a valid returned-item condition.")
            return redirect("requests" if is_cashier(request.user) else "requests")
        if req.return_suit(performed_by=request.user, return_condition=return_condition) and req.late_fee > 0:
            messages.success(request, f"Suit returned successfully. Late fee applied: ${req.late_fee}.")
        elif req.status == "Returned":
            messages.success(request, "Suit returned successfully.")
        else:
            messages.error(request, "This rental has already been returned.")
    else:
        messages.error(request, "This rental has already been returned.")

    if is_cashier(request.user):
        return redirect("requests")
    return redirect("requests")


@login_required
@transaction.atomic
def process_return(request):
    if request.method != "POST":
        return redirect("requests")

    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    return_page = "requests" if is_admin(request.user) else "requests"
    rental_id = request.POST.get("rental_id")
    req = get_object_or_404(
        SuitRequest.objects.select_for_update().select_related("suit"),
        id=rental_id,
    )
    if not is_admin(request.user):
        return_page = reverse("return_view", kwargs={"request_id": req.pk})
    if req.status != "Active":
        messages.error(request, "Only active rentals can be returned.")
        return redirect("requests" if not is_admin(request.user) else return_page)

    return_condition = request.POST.get("return_condition", "good")
    if return_condition not in dict(SuitRequest.RETURN_CONDITION_CHOICES):
        messages.error(request, "Select a valid returned-item condition.")
        return redirect(return_page)

    def read_nonnegative_amount(field_name):
        try:
            amount = Decimal(request.POST.get(field_name, "0") or "0")
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not amount.is_finite() or amount < 0:
            return None
        return amount.quantize(Decimal("0.01"))

    discount = read_nonnegative_amount("return_discount")
    amount_received = read_nonnegative_amount("return_amount_paid")
    damage_fee = read_nonnegative_amount("laundry_fee") if request.POST.get("requires_laundry") else Decimal("0.00")
    if discount is None or amount_received is None or damage_fee is None:
        messages.error(request, "Enter valid, non-negative amounts for the return.")
        return redirect(return_page)

    actual_return_date_str = request.POST.get("actual_return_date", "")
    return_dt = parse_datetime(actual_return_date_str) if actual_return_date_str else None
    if return_dt is None and actual_return_date_str:
        parsed_date = parse_date(actual_return_date_str)
        if parsed_date:
            return_dt = datetime.combine(parsed_date, timezone.localtime().time())
    if return_dt is None:
        return_dt = timezone.localtime()
    if timezone.is_naive(return_dt):
        return_dt = timezone.make_aware(return_dt, timezone.get_current_timezone())
    if return_dt > timezone.now():
        messages.error(request, "Return date and time cannot be in the future.")
        return redirect(return_page)
    if req.rental_start_time and return_dt < req.rental_start_time:
        messages.error(request, "Return date and time cannot be before the rental started.")
        return redirect(return_page)

    late_fee = req._calculate_late_fee(return_dt)
    guarantee_settlement = settle_guarantee(req.guarantee_collected, damage_fee)
    uncovered_damage_fee = guarantee_settlement["uncovered_damage"]
    total_before_discount = (req.total_amount or Decimal("0.00")) + late_fee + uncovered_damage_fee
    if discount > total_before_discount:
        messages.error(request, "Discount cannot be greater than the amount due.")
        return redirect(return_page)

    final_total = total_before_discount - discount
    current_balance = max(final_total - req.amount_paid, Decimal("0.00"))
    if amount_received > current_balance:
        messages.error(request, "Amount received cannot be greater than the remaining balance.")
        return redirect(return_page)

    if current_balance > 0 and amount_received == 0:
        messages.error(request, "Enter an amount received before recording a partial payment.")
        return redirect(return_page)

    if amount_received < current_balance and (discount or damage_fee):
        messages.error(request, "Discounts and damage fees can only be applied when the return is paid in full.")
        return redirect(return_page)

    idempotency_key_raw = request.POST.get("payment_idempotency_key", "").strip()
    idempotency_key = None
    if idempotency_key_raw:
        try:
            idempotency_key = str(UUID(idempotency_key_raw))
        except (TypeError, ValueError, AttributeError):
            messages.error(request, "Invalid payment submission key. Reload the return form and try again.")
            return redirect(return_page)

        previous_payment = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if previous_payment:
            same_submission = (
                previous_payment.rental_id == req.pk
                and previous_payment.amount_paid == amount_received
                and previous_payment.payment_method == request.POST.get("payment_method", "cash")
                and previous_payment.payment_reference == request.POST.get("transaction_ref", "")
            )
            if same_submission:
                messages.info(request, "This payment was already recorded; it was not added again.")
            else:
                messages.error(request, "This payment submission key has already been used.")
            return redirect(return_page)

    def create_return_payment(status, notes):
        payment_values = {
            "rental": req,
            "cashier": request.user,
            "amount_paid": amount_received,
            "payment_method": request.POST.get("payment_method", "cash"),
            "payment_reference": request.POST.get("transaction_ref", ""),
            "status": status,
            "notes": notes,
        }
        if idempotency_key:
            payment, created = Payment.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults=payment_values,
            )
        else:
            payment = Payment.objects.create(**payment_values)
            created = True

        if not created:
            same_submission = (
                payment.rental_id == req.pk
                and payment.amount_paid == amount_received
                and payment.payment_method == request.POST.get("payment_method", "cash")
                and payment.payment_reference == request.POST.get("transaction_ref", "")
            )
            if not same_submission:
                return None, False

        if created and not payment.receipt_number:
            payment.receipt_number = f"RCPT-{payment.id:06d}"
            payment.save(update_fields=["receipt_number"])
        return payment, created

    if amount_received < current_balance:
        payment, created = create_return_payment(
            "partial",
            "Partial collection before return completion.",
        )
        if not created:
            if payment:
                messages.info(request, "This payment was already recorded; it was not added again.")
            else:
                messages.error(request, "This payment submission key has already been used.")
            return redirect(return_page)
        messages.info(request, f"Partial payment recorded. Remaining balance: ${max(current_balance - amount_received, Decimal('0.00')):.2f}. Return is not complete.")
        return redirect(return_page)

    with transaction.atomic():
        if amount_received:
            payment, created = create_return_payment(
                "paid",
                f"Return collection. Discount applied: {discount:.2f}",
            )
            if not created:
                if payment:
                    messages.info(request, "This payment was already recorded; it was not added again.")
                else:
                    messages.error(request, "This payment submission key has already been used.")
                return redirect(return_page)

        req.total_amount = final_total
        req.return_date = return_dt
        req.guarantee_refunded = guarantee_settlement["refund"]
        req.guarantee_settled_at = timezone.now()
        returned_items = (request.POST.get("returned_items") or "").strip()
        return_notes = (request.POST.get("return_notes") or "").strip()
        if returned_items:
            req.notes = f"{req.notes}\nReturned items: {returned_items}" if req.notes else f"Returned items: {returned_items}"
        if return_notes:
            req.notes = f"{req.notes}\nReturn notes: {return_notes}" if req.notes else f"Return notes: {return_notes}"
        req.status = "Returned"
        req.return_condition = return_condition
        req.returned_by = request.user
        req.save(update_fields=[
            "total_amount", "return_date", "notes", "status",
            "return_condition", "returned_by", "guarantee_refunded",
            "guarantee_settled_at",
        ])
        if guarantee_settlement["refund"] > 0:
            refund = Payment.objects.create(
                rental=req,
                cashier=request.user,
                amount_paid=Decimal("0.00"),
                payment_method=request.POST.get("payment_method", "cash"),
                refund_amount=guarantee_settlement["refund"],
                refund_method=request.POST.get("payment_method", "cash"),
                refund_date=timezone.now(),
                status="refunded",
                notes=(
                    "Refund of accessory guarantee. "
                    f"Damage retained: {guarantee_settlement['kept']:.2f}."
                ),
            )
            refund.receipt_number = f"REF-{refund.id:06d}"
            refund.save(update_fields=["receipt_number"])
        record_late_fee_transaction(
            req,
            late_fee,
            return_dt,
            performed_by=request.user,
        )

        suit = req.suit
        if suit:
            if suit.should_queue_for_dry_cleaning or return_condition == "damaged":
                suit.status = "Dry Cleaning"
                movement_type = "returned_for_cleaning"
                quantity_delta = 0
            else:
                suit.quantity += req.quantity_requested or 1
                suit.status = "Available"
                movement_type = "returned"
                quantity_delta = req.quantity_requested or 1
            suit.save(update_fields=["status", "quantity"])
            record_inventory_movement(
                suit,
                movement_type,
                quantity_delta=quantity_delta,
                performed_by=request.user,
                rental_request=req,
                notes=(
                    "Damaged item held out of available stock pending cleaning or repair."
                    if return_condition == "damaged"
                    else "Customer return processed."
                ),
            )
            suit.refresh_rental_end_time()

        req.is_paid = req.balance_due <= 0
        req.save(update_fields=["is_paid"])

    if suit and suit.should_queue_for_dry_cleaning:
        messages.success(request, f"Return complete. {suit.suit_name} is queued for cleaning.")
    elif suit:
        messages.success(request, f"Return complete. {suit.suit_name} is back in stock.")
    else:
        messages.success(request, "Accessory-only return completed.")
    return redirect("laundry_backlog")


@login_required
def process_reservation(request):
    if request.method != 'POST':
        return redirect('requests')

    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    return_url = 'requests' if is_admin(request.user) else 'requests'

    rental_id = request.POST.get("rental_id")
    duration_hours = request.POST.get("duration_hours", "24")

    if not rental_id:
        messages.error(request, "Please select a reservation to process.")
        return redirect(return_url)

    req = get_object_or_404(SuitRequest, id=rental_id)

    if req.status not in ["Pending", "Approved"]:
        messages.error(request, "Only pending or approved requests can be placed on hold.")
        return redirect(return_url)

    try:
        duration_hours = int(duration_hours)
    except (TypeError, ValueError):
        duration_hours = 24

    if duration_hours <= 0:
        messages.error(request, "Reservation duration must be greater than zero.")
        return redirect(return_url)

    try:
        if not req.reserve_inventory(timezone.now() + timedelta(hours=duration_hours), performed_by=request.user):
            messages.error(request, "This request already has an active inventory reservation.")
            return redirect(return_url)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(return_url)

    req.notes = (req.notes or "") + f"\nReserved for {duration_hours} hours."
    req.save(update_fields=['notes'])

    messages.success(request, "Rental request has been reserved successfully.")
    return redirect(return_url)


@login_required
def reception_return_view(request, rental_id):
    if not (is_reception(request.user) or is_cashier(request.user)):
        messages.error(request, "Access denied. Reception staff only.")
        return redirect("dashboard" if is_admin(request.user) else "home")

    selected_rental = get_object_or_404(
        SuitRequest.objects.select_related("suit"),
        pk=rental_id,
        status="Active",
    )
    active_rentals = SuitRequest.objects.filter(status="Active").select_related("suit").order_by("-request_date")
    return render(request, "reception/reception_return.html", {
        "active_rentals": active_rentals,
        "selected_rental": selected_rental,
        "late_fee_policy": RentalPolicy.get_solo(),
        "return_payment_key": str(uuid4()),
        "current_return_time": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
    })


@login_required
@transaction.atomic
def return_suit_view(request, request_id):
    """Simple endpoint to mark a suit request as returned and enqueue it for cleaning.

    This mirrors the lightweight behavior in the snippet provided by the user:
    - Marks the request `Returned` if it was previously Approved/Active
    - Does NOT modify suit quantity here (cleaning will restore availability)
    - Redirects back to the reception requests page.
    """
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    suit_request = get_object_or_404(
        SuitRequest.objects.select_for_update().select_related("suit"),
        id=request_id,
    )
    if suit_request.guarantee_collected and not suit_request.guarantee_settled_at:
        messages.error(request, "Use the detailed return form to settle the accessory guarantee.")
        return redirect("requests")
    if suit_request.return_suit(performed_by=request.user):
        if suit_request.suit and suit_request.suit.should_queue_for_dry_cleaning:
            messages.info(request, f"{suit_request.suit.suit_name} was received and queued for cleaning.")
        elif suit_request.suit:
            messages.info(request, f"{suit_request.suit.suit_name} was returned to inventory.")
        else:
            messages.info(request, "Accessory-only rental returned.")
    else:
        messages.error(request, "Only active rentals can be returned.")

    return redirect('requests')


@login_required
@transaction.atomic
def return_suit_audit_view(request, request_id):
    """Show an audit/receive page before confirming a return and sending to laundry.

    Displays total, paid and remaining debt and on POST marks the request `Returned`
    and redirects to the laundry backlog page.
    """
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    suit_request = get_object_or_404(
        SuitRequest.objects.select_for_update().select_related("suit"),
        id=request_id,
    )
    if suit_request.guarantee_collected and not suit_request.guarantee_settled_at:
        messages.error(request, "Use the detailed return form to settle the accessory guarantee.")
        return redirect("requests")

    # Calculate debt: price_per_day * days_requested * quantity_requested
    total_amount = suit_request.total_amount
    amount_paid = getattr(suit_request, 'amount_paid', 0) or 0
    try:
        remaining_debt = total_amount - amount_paid
    except Exception:
        remaining_debt = total_amount

    if request.method == 'POST':
        if not suit_request.return_suit(performed_by=request.user):
            messages.error(request, "Only active rentals can be returned.")
            return redirect('requests')
        if suit_request.suit and suit_request.suit.should_queue_for_dry_cleaning:
            messages.success(request, f"{suit_request.suit.suit_name} was received and queued for cleaning.")
        elif suit_request.suit:
            messages.success(request, f"{suit_request.suit.suit_name} was returned to inventory.")
        else:
            messages.success(request, "Accessory-only rental returned.")
        return redirect('laundry_backlog')

    context = {
        'req': suit_request,
        'total_amount': total_amount,
        'amount_paid': amount_paid,
        'remaining_debt': remaining_debt,
    }
    return render(request, 'reception/return_audit.html', context)


# =========================
# ADD SUIT
# =========================
@login_required
def add_suit(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    if request.method == "POST":
        image_file = request.FILES.get("image") or request.FILES.get("suit_image")
        category = request.POST.get("category", "suits")
        collection = request.POST.get("collection")
        price = float(request.POST.get("price_per_day", 0))
        description = request.POST.get("description", "").strip()
        condition_grade = request.POST.get("condition_grade") or "new"
        damage_notes = request.POST.get("damage_notes", "").strip()

        if image_file:
            try:
                if not is_admin(request.user):
                    validate_image_upload(image_file, "suit image")
            except forms.ValidationError as e:
                messages.error(request, str(e))
                return redirect("add_suit")

        if price < 0:
            messages.error(request, "Price must be a positive number.")
            return redirect("add_suit")

        suit_name = request.POST.get("suit_name", "").strip()
        if not suit_name:
            messages.error(request, "Suit name is required.")
            return redirect("add_suit")

        # Price validation based on collection
        if collection == "Luxury":
            if not (25 <= price <= 100):
                messages.error(request, "Luxury suits price must be between $25 and $100.")
                return redirect("add_suit")
        elif collection == "Standard":
            if not (15 <= price <= 25):
                messages.error(request, "Standard suits price must be between $15 and $25.")
                return redirect("add_suit")
        elif collection == "Budget":
            if not (14 <= price <= 18):
                messages.error(request, "Budget suits price must be between $14 and $18.")
                return redirect("add_suit")

        suit_id = request.POST.get("suit_id")
        if suit_id:
            try:
                suit = Suit.objects.get(id=suit_id)
            except Suit.DoesNotExist:
                messages.error(request, "Suit not found.")
                return redirect("inventory")

            if not _set_inventory_quantity(request, suit, request.POST.get("quantity", suit.quantity)):
                return redirect("inventory")
            suit.refresh_from_db()
            suit.suit_name = request.POST.get("suit_name", suit.suit_name)
            suit.collection = collection or suit.collection
            suit.description = description or suit.description
            suit.size = request.POST.get("size", suit.size)
            suit.item_type = category or suit.item_type or "suits"
            suit.color = request.POST.get("color", suit.color)
            suit.price_per_day = price
            suit.condition_grade = condition_grade
            suit.damage_notes = damage_notes
            if image_file:
                suit.image = image_file
            suit.save()
            messages.success(request, "Suit updated successfully!")
        else:
            suit = Suit.objects.create(
                suit_name=request.POST["suit_name"],
                collection=collection,
                description=description,
                size=request.POST["size"],
                item_type=category,
                color=request.POST["color"],
                price_per_day=price,
                quantity=int(request.POST.get("quantity", 1)),
                image=image_file,
                condition_grade=condition_grade,
                damage_notes=damage_notes,
                status="Available"
            )
            record_inventory_movement(
                suit,
                "opening_balance",
                quantity_delta=suit.quantity,
                performed_by=request.user,
                notes="Opening stock entered when the item was added.",
            )
            messages.success(request, "Suit added successfully!")

        if is_reception(request.user) or is_cashier(request.user):
            return redirect("inventory")
        return redirect("inventory")

    suits = Suit.objects.all().order_by('-id')
    query = request.GET.get('q', '').strip()
    if query:
        suits = suits.filter(
            Q(suit_name__icontains=query) |
            Q(size__icontains=query) |
            Q(color__icontains=query) |
            Q(description__icontains=query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter and status_filter != 'all':
        if status_filter == 'available':
            suits = _available_inventory_queryset(suits)
        elif status_filter == 'rented':
            suits = suits.filter(status__iexact='Rented')
        elif status_filter == 'out':
            suits = suits.filter(quantity__lte=0)

    # Apply category keyword filtering when a specific category is requested
    selected_category = request.GET.get('category', 'all').strip().lower()
    if selected_category != 'all':
        if selected_category == 'accessories':
            suits = suits.filter(
                Q(item_type__in=['ties', 'shoes', 'belts', 'accessories']) |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='tie') | Q(suit_name__icontains='belt') | Q(suit_name__icontains='shoe') |
                Q(description__icontains='tie') | Q(description__icontains='belt') | Q(description__icontains='shoe') |
                Q(description__icontains='accessory')
            )
        elif selected_category == 'shirts':
            suits = suits.filter(
                Q(item_type__iexact='shirts') |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='shirt') | Q(description__icontains='shirt')
            )
        elif selected_category == 'suits':
            suits = suits.filter(
                Q(item_type__iexact='suits') |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='suit') | Q(description__icontains='suit')
            )
        else:
            category_terms = {
                'shoes': ['shoe', 'shoes'],
                'ties': ['tie', 'ties'],
                'belts': ['belt', 'belts'],
            }
            terms = category_terms.get(selected_category, [])
            if terms:
                cat_q = Q()
                for t in terms:
                    cat_q |= Q(suit_name__icontains=t) | Q(description__icontains=t)
                suits = suits.filter(cat_q)

    total_suits = Suit.objects.count()
    available_count = _available_inventory_queryset().count()
    rented_count = Suit.objects.filter(status__iexact='Rented').count()
    out_of_stock_count = Suit.objects.filter(quantity__lte=0).count()
    total_value = sum((s.price_per_day * s.quantity) for s in Suit.objects.all())

    category_counts = {
        'all': total_suits,
        'suits': Suit.objects.filter(Q(item_type__iexact='suits') | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='suit') | Q(description__icontains='suit'))).count(),
        'shirts': Suit.objects.filter(Q(item_type__iexact='shirts') | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='shirt') | Q(description__icontains='shirt'))).count(),
        'ties': Suit.objects.filter(Q(item_type__iexact='ties') | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='tie') | Q(description__icontains='tie'))).count(),
        'shoes': Suit.objects.filter(Q(item_type__iexact='shoes') | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='shoe') | Q(description__icontains='shoe'))).count(),
        'belts': Suit.objects.filter(Q(item_type__iexact='belts') | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='belt') | Q(description__icontains='belt'))).count(),
        'accessories': Suit.objects.filter(Q(item_type__in=['ties', 'shoes', 'belts', 'accessories']) | (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='tie') | Q(suit_name__icontains='belt') | Q(suit_name__icontains='shoe') | Q(description__icontains='tie') | Q(description__icontains='belt') | Q(description__icontains='shoe') | Q(description__icontains='accessory'))).count(),
    }

    context = {
        'suits': suits,
        'query': query,
        'status_filter': status_filter,
        'total_suits': total_suits,
        'available_count': available_count,
        'rented_count': rented_count,
        'out_of_stock_count': out_of_stock_count,
        'total_value': total_value,
        'category_counts': category_counts,
    }

    return render(request, "admin/add_suit.html", context)


# =========================
# EDIT SUIT
# =========================
@login_required
def edit_suit(request, suit_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    suit = get_object_or_404(Suit, id=suit_id)

    if request.method == "POST":
        requested_status = request.POST.get("status", suit.status)
        if not is_admin(request.user) and requested_status == "Dry Cleaning" and suit.status != "Dry Cleaning":
            messages.error(request, "Reception must request laundry approval instead of directly changing this status.")
            return redirect("inventory")

        collection = request.POST["collection"]
        price = float(request.POST["price_per_day"])

        image_file = request.FILES.get("image")
        if image_file:
            try:
                if not is_admin(request.user):
                    validate_image_upload(image_file, "suit image")
            except forms.ValidationError as e:
                messages.error(request, str(e))
                return redirect("edit_suit", suit_id=suit_id)

        # Price validation based on collection
        if collection == "Luxury":
            if not (25 <= price <= 100):
                messages.error(request, "Luxury suits price must be between $25 and $100.")
                return redirect("edit_suit", suit_id=suit_id)
        elif collection == "Standard":
            if not (15 <= price <= 25):
                messages.error(request, "Standard suits price must be between $15 and $25.")
                return redirect("edit_suit", suit_id=suit_id)
        elif collection == "Budget":
            if not (14 <= price <= 18):
                messages.error(request, "Budget suits price must be between $14 and $18.")
                return redirect("edit_suit", suit_id=suit_id)

        if not _set_inventory_quantity(request, suit, request.POST.get("quantity", suit.quantity)):
            return redirect("edit_suit", suit_id=suit_id)

        suit.suit_name = request.POST["suit_name"]
        suit.collection = collection
        suit.size = request.POST["size"]
        suit.item_type = request.POST.get("category", suit.item_type or "suits")
        suit.color = request.POST["color"]
        suit.price_per_day = price
        suit.status = request.POST["status"]

        if request.FILES.get("image"):
            suit.image = request.FILES["image"]
        elif request.POST.get("clear_image"):
            suit.image = None

        suit.save()
        messages.success(request, "Suit updated successfully!")
        if is_reception(request.user) or is_cashier(request.user):
            return redirect("inventory")
        return redirect("dashboard")

    return render(request, "admin/edit_suit.html", {"suit": suit})


# =========================
# DELETE SUIT
# =========================
@login_required
def delete_suit(request, suit_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    Suit.objects.get(id=suit_id).delete()
    if is_reception(request.user) or is_cashier(request.user):
        return redirect("inventory")
    return redirect("dashboard")


# =========================
# INVENTORY MANAGEMENT
# =========================
@login_required
def inventory_management(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    sort_by = request.GET.get('sort', 'size')
    order = request.GET.get('order', 'asc')
    query = request.GET.get('q', '').strip()
    selected_category = request.GET.get('category', 'all').strip().lower() or 'all'
    status_filter = request.GET.get('status', '').strip()
    page_number = request.GET.get('page', 1)

    suits = Suit.objects.all().order_by('-id')

    if query:
        suits = suits.filter(
            Q(suit_name__icontains=query) |
            Q(size__icontains=query) |
            Q(color__icontains=query) |
            Q(collection__icontains=query)
        )

    if selected_category != 'all':
        category_terms = {
            'suits': ['suit'],
            'shirts': ['shirt', 'shirts', 't-shirt', 'tshirts', 'polo'],
            'accessories': ['tie', 'ties', 'belt', 'belts', 'shoe', 'shoes', 'accessory', 'accessories', 'vest', 'cufflink', 'cufflinks', 'suspenders', 'sock', 'socks'],
        }
        terms = category_terms.get(selected_category, [])
        if terms:
            category_filter = Q()
            for term in terms:
                category_filter |= Q(suit_name__icontains=term) | Q(description__icontains=term)
            suits = suits.filter(category_filter)

    if status_filter and status_filter != 'all':
        if status_filter == 'available':
            suits = _available_inventory_queryset(suits)
        elif status_filter == 'rented':
            suits = suits.filter(Q(status__iexact='Rented') | Q(status__iexact='Reserved'))
        elif status_filter == 'out':
            suits = suits.filter(quantity__lte=0)
        elif status_filter == 'retired':
            suits = suits.filter(condition_grade='retired')

    if sort_by == 'size':
        suits = suits.order_by('size' if order == 'asc' else '-size')
    elif sort_by == 'color':
        suits = suits.order_by('color' if order == 'asc' else '-color')
    elif sort_by == 'price':
        suits = suits.order_by('price_per_day' if order == 'asc' else '-price_per_day')
    elif sort_by == 'quantity':
        suits = suits.order_by('quantity' if order == 'asc' else '-quantity')

    total_items = Suit.objects.count()
    available_count = _available_inventory_queryset().count()
    rented_count = SuitRequest.objects.filter(status__in=["Active", "Reserved"]).aggregate(total=Sum('quantity_requested'))['total'] or 0
    out_of_stock_count = Suit.objects.filter(quantity__lte=0).count()
    total_value = sum((s.price_per_day * s.quantity) for s in Suit.objects.all())

    for suit in suits:
        suit.rented_quantity = suit.suitrequest_set.filter(status='Active').aggregate(total=Sum('quantity_requested'))['total'] or 0
        suit.stock_width_percent = 100 if suit.quantity > 0 else 0
        suit.stock_color = 'var(--success)' if suit.quantity > 0 else 'var(--danger)'
        suit.net_earnings = suit.total_earnings or Decimal('0.00')
        suit.cleaning_cost_total = Decimal('0.00')

    paginator = Paginator(suits, 10)
    page_obj = paginator.get_page(page_number)
    suits = page_obj.object_list

    retired_count = Suit.objects.filter(condition_grade='retired').count()
    total_quantity = Suit.objects.aggregate(total=Sum('quantity'))['total'] or 0

    # Category counts (for UI pills)
    suits_count = Suit.objects.filter(
        Q(suit_name__icontains="suit") | Q(description__icontains="suit")
    ).count()
    shirts_count = Suit.objects.filter(
        Q(suit_name__icontains="shirt") | Q(description__icontains="shirt")
    ).count()
    accessories_count = Suit.objects.filter(
        Q(suit_name__icontains="tie") | Q(suit_name__icontains="belt") | Q(suit_name__icontains="shoe") |
        Q(description__icontains="tie") | Q(description__icontains="belt") | Q(description__icontains="shoe")
    ).count()

    context = {
        'suits': suits,
        'is_admin_user': is_admin(request.user),
        'page_obj': page_obj,
        'page_range': list(range(max(1, page_obj.number - 2), min(page_obj.paginator.num_pages, page_obj.number + 2) + 1)),
        'sort_by': sort_by,
        'order': order,
        'query': query,
        'selected_category': selected_category,
        'status_filter': status_filter,
        'total_items': total_items,
        'available_count': available_count,
        'rented_count': rented_count,
        'out_of_stock_count': out_of_stock_count,
        'retired_count': retired_count,
        'total_quantity': total_quantity,
        'total_value': total_value,
        'category_counts': {
            'all': total_items,
            'suits': suits_count,
            'shirts': shirts_count,
            'accessories': accessories_count,
        },
    }

    return render(request, "admin/inventory_admin.html", context)


@login_required
def inventory_movement_history(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    movements = InventoryMovement.objects.select_related(
        "suit", "rental_request", "performed_by"
    )
    query = request.GET.get("q", "").strip()
    movement_type = request.GET.get("type", "").strip()
    if query:
        movements = movements.filter(
            Q(suit_name__icontains=query)
            | Q(rental_request__name__icontains=query)
            | Q(rental_request__phone__icontains=query)
            | Q(notes__icontains=query)
        )
    if movement_type in dict(InventoryMovement.MOVEMENT_TYPES):
        movements = movements.filter(movement_type=movement_type)

    page_obj = Paginator(movements, 30).get_page(request.GET.get("page"))
    return render(request, "admin/inventory_movements.html", {
        "page_obj": page_obj,
        "movements": page_obj.object_list,
        "query": query,
        "selected_type": movement_type,
        "movement_types": InventoryMovement.MOVEMENT_TYPES,
    })


@login_required
def inventory_delete_suit(request):
    """Handle POST delete requests from the admin inventory UI modal.

    This accepts a form POST containing `suit_id` so the template can
    submit deletions without encoding the id into the URL path.
    """
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("inventory")

    suit_id = request.POST.get("suit_id")
    if not suit_id:
        messages.error(request, "No suit specified to delete.")
        return redirect("inventory")

    suit = get_object_or_404(Suit, id=suit_id)
    suit.delete()
    messages.success(request, f"{suit.suit_name} has been deleted from inventory.")
    return redirect("inventory")


@login_required
def retire_suit(request, suit_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method != 'POST':
        messages.error(request, "Invalid request method.")
        return redirect("inventory")

    suit = get_object_or_404(Suit, id=suit_id)
    suit.condition_grade = 'retired'
    suit.status = 'Available' if suit.quantity > 0 else suit.status
    suit.save(update_fields=['condition_grade', 'status'])
    messages.success(request, f"{suit.suit_name} has been retired.")
    return redirect("inventory")


@login_required
def restore_suit(request, suit_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method != 'POST':
        messages.error(request, "Invalid request method.")
        return redirect("inventory")

    suit = get_object_or_404(Suit, id=suit_id)
    suit.condition_grade = 'new'
    suit.status = 'Available' if suit.quantity > 0 else 'Pending Request'
    suit.save(update_fields=['condition_grade', 'status'])
    messages.success(request, f"{suit.suit_name} has been restored to inventory.")
    return redirect("inventory")

    return redirect("inventory")


@login_required
def reception_inventory(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    suits = Suit.objects.all().order_by('-id')

    query = request.GET.get('q', '').strip()
    if query:
        suits = suits.filter(
            Q(suit_name__icontains=query) |
            Q(size__icontains=query) |
            Q(color__icontains=query) |
            Q(collection__icontains=query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter:
        suits = suits.filter(status__iexact=status_filter)

    # Paginate suits (10 per page)
    from django.core.paginator import Paginator
    paginator = Paginator(suits, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    total_suits = Suit.objects.count()
    available_count = Suit.objects.filter(status__iexact='Available').count()
    rented_count = Suit.objects.filter(status__iexact='Rented').count()
    cleaning_count = Suit.objects.filter(status__iexact='Cleaning').count()

    # Category counts for reception inventory UI
    suits_count = Suit.objects.filter(
        Q(suit_name__icontains="suit") | Q(description__icontains="suit")
    ).count()
    shirts_count = Suit.objects.filter(
        Q(suit_name__icontains="shirt") | Q(description__icontains="shirt")
    ).count()
    accessories_count = Suit.objects.filter(
        Q(suit_name__icontains="tie") | Q(suit_name__icontains="belt") | Q(suit_name__icontains="shoe") |
        Q(description__icontains="tie") | Q(description__icontains="belt") | Q(description__icontains="shoe")
    ).count()

    context = {
        'suits': suits,
        'page_obj': page_obj,
        'query': query,
        'status_filter': status_filter,
        'total_suits': total_suits,
        'available_count': available_count,
        'rented_count': rented_count,
        'cleaning_count': cleaning_count,
        'status_choices': getattr(Suit, 'STATUS_CHOICES', [
            ('Available', 'Available'),
            ('Rented', 'Rented'),
            ('Cleaning', 'Cleaning'),
            ('Retired', 'Retired'),
        ]),
        'category_counts': {
            'all': total_suits,
            'suits': suits_count,
            'shirts': shirts_count,
            'accessories': accessories_count,
        },
    }

    return render(request, "reception/reception_inventory.html", context)


@login_required
def export_inventory(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="suit_inventory.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'Name', 'Collection', 'Size', 'Color', 'Price/Day', 'Quantity', 'Status'])

    for suit in Suit.objects.all():
        writer.writerow([
            suit.id,
            suit.suit_name,
            suit.collection,
            suit.size,
            suit.color,
            suit.price_per_day,
            suit.quantity,
            suit.status,
        ])

    return response


# =========================
# ADMIN PROFILE
# =========================
@login_required
def admin_profile(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    late_fee_policy = RentalPolicy.get_solo()
    if request.method == "POST":
        try:
            late_fee_per_hour = Decimal(request.POST.get("late_fee_per_hour", ""))
        except (InvalidOperation, TypeError, ValueError):
            late_fee_per_hour = None

        partial_hour_policy = request.POST.get("partial_hour_policy", "")
        valid_policy = partial_hour_policy in dict(RentalPolicy.PARTIAL_HOUR_CHOICES)
        if (
            late_fee_per_hour is None
            or not late_fee_per_hour.is_finite()
            or late_fee_per_hour < 0
            or late_fee_per_hour > Decimal("999999.99")
            or not valid_policy
        ):
            messages.error(request, "Enter a valid non-negative hourly rate and partial-hour rule.")
        else:
            late_fee_policy.late_fee_per_hour = late_fee_per_hour.quantize(Decimal("0.01"))
            late_fee_policy.partial_hour_policy = partial_hour_policy
            late_fee_policy.save(update_fields=["late_fee_per_hour", "partial_hour_policy", "updated_at"])
            messages.success(request, "Late-fee policy saved. Existing assessed fees are unchanged.")
            return redirect("admin_profile")

    return render(request, "admin/admin_profile.html", {
        "late_fee_policy": late_fee_policy,
        "late_fee_choices": RentalPolicy.PARTIAL_HOUR_CHOICES,
    })


@login_required(login_url='customer_login')
def toggle_favorite(request, suit_id):
    suit = get_object_or_404(Suit, id=suit_id)
    favorite, created = FavoriteSuit.objects.get_or_create(user=request.user, suit=suit)
    if not created:
        favorite.delete()
        messages.info(request, f"Removed {suit.suit_name} from favorites.")
    else:
        messages.success(request, f"Added {suit.suit_name} to favorites.")

    return redirect(request.META.get('HTTP_REFERER') or reverse('all_suits'))


# =========================
# REGISTER
# =========================
def register_view(request):
    if request.method == "POST":
        from .forms import UserRegistrationForm
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            try:
                with transaction.atomic():
                    user = User.objects.create_user(username=username, email=email, password=password)
                    # ensure a Profile exists for the new user
                    Profile.objects.get_or_create(user=user)
                messages.success(request, "Registration successful! You are now logged in.")
                # log the user in
                login(request, user)
                return redirect("home")
            except IntegrityError:
                messages.error(request, "An error occurred during registration. Please try again.")
                return redirect("register")
        else:
            # Display form errors
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, str(error))
            return redirect("register")
    
    return render(request, "customer/register.html")


# =========================
# REPORTS
# =========================
def _get_report_data(request):
    """Return the validated report range and all report querysets for that range."""
    today = timezone.localdate()
    start_date_raw = request.GET.get("start_date", request.GET.get("from_date", "")).strip()
    end_date_raw = request.GET.get("end_date", request.GET.get("to_date", "")).strip()

    # Preserve support for the former single-day report filter.
    legacy_date = request.GET.get("date", "").strip()
    if not start_date_raw and not end_date_raw and legacy_date:
        start_date_raw = end_date_raw = legacy_date

    date_range_error = None
    if not start_date_raw and not end_date_raw:
        start_date = today.replace(day=1)
        end_date = today
    elif not start_date_raw or not end_date_raw:
        start_date = end_date = None
        date_range_error = "Both start and end dates are required when filtering."
    else:
        try:
            start_date = parse_date(start_date_raw)
            end_date = parse_date(end_date_raw)
        except (TypeError, ValueError):
            start_date = end_date = None

        if start_date is None:
            date_range_error = "Enter a valid start date."
        elif end_date is None:
            date_range_error = "Enter a valid end date."
        elif start_date > today:
            date_range_error = "Start date cannot be in the future."
        elif end_date > today:
            date_range_error = "End date cannot be in the future."
        elif start_date > end_date:
            date_range_error = "Start date must be on or before the end date."

    if date_range_error:
        start_value = start_date_raw
        end_value = end_date_raw
        rentals = SuitRequest.objects.none()
        payments = Payment.objects.none()
        expenses = Expense.objects.none()
    else:
        start_value = start_date.isoformat()
        end_value = end_date.isoformat()
        current_tz = timezone.get_current_timezone()
        range_start = timezone.make_aware(datetime.combine(start_date, time.min), current_tz)
        range_end = timezone.make_aware(datetime.combine(end_date, time.max), current_tz)
        rentals = SuitRequest.objects.filter(
            request_date__range=(range_start, range_end)
        ).select_related("suit").order_by("-request_date")
        payments = Payment.objects.filter(
            payment_date__range=(range_start, range_end)
        ).select_related("rental", "rental__suit", "cashier").order_by("-payment_date")
        expenses = Expense.objects.filter(
            expense_date__range=(range_start, range_end)
        ).select_related("created_by").order_by("-expense_date")

    return {
        "today": today,
        "start_date": start_value,
        "end_date": end_value,
        "date_range_error": date_range_error,
        "rentals": rentals,
        "payments": payments,
        "expenses": expenses,
    }


@login_required
def reports(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    report_data = _get_report_data(request)
    rentals = report_data["rentals"]
    payments = report_data["payments"]
    expenses = report_data["expenses"]

    rental_summary = rentals.aggregate(
        rental_count=Count("id"),
    )
    revenue = rentals.filter(status__in=["Active", "Returned"]).aggregate(
        total=Sum("total_amount")
    )["total"] or Decimal("0.00")
    expense_summary = expenses.aggregate(
        total=Sum("amount"),
        expense_count=Count("id"),
    )
    total_expenses = expense_summary["total"] or Decimal("0.00")

    rental_by_day = list(
        rentals.annotate(day=TruncDate("request_date", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(rental_count=Count("id"))
        .order_by("day")
    )
    revenue_by_day = list(
        rentals.filter(status__in=["Active", "Returned"])
        .annotate(day=TruncDate("request_date", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(total=Sum("total_amount"))
        .order_by("day")
    )
    expense_by_day = list(
        expenses.annotate(day=TruncDate("expense_date", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )
    payments_by_day = list(
        payments.annotate(day=TruncDate("payment_date", tzinfo=timezone.get_current_timezone()))
        .values("day")
        .annotate(payment_count=Count("id"))
        .order_by("day")
    )

    chart_days = sorted({
        row["day"]
        for rows in (rental_by_day, revenue_by_day, expense_by_day, payments_by_day)
        for row in rows
    })
    revenue_values = {row["day"]: row["total"] for row in revenue_by_day}
    expense_values = {row["day"]: row["total"] for row in expense_by_day}
    rental_counts = {row["day"]: row["rental_count"] for row in rental_by_day}
    payment_counts = {row["day"]: row["payment_count"] for row in payments_by_day}
    chart_labels = [day.strftime("%b %d") for day in chart_days]
    chart_financial = {
        "labels": chart_labels,
        "revenue": [float(revenue_values.get(day, 0) or 0) for day in chart_days],
        "expenses": [float(expense_values.get(day, 0) or 0) for day in chart_days],
    }
    chart_activity = {
        "labels": chart_labels,
        "rentals": [rental_counts.get(day, 0) for day in chart_days],
        "payments": [payment_counts.get(day, 0) for day in chart_days],
    }

    activities = []
    for rental in rentals[:100]:
        activities.append({
            "type": "rental",
            "date": rental.request_date,
            "details": f"{rental.name} · {rental.suit.suit_name if rental.suit else 'Suit unavailable'} · {rental.status}",
            "reference": f"REQ-{rental.pk}",
            "amount": rental.total_amount,
            "search": f"{rental.name} {rental.suit.suit_name if rental.suit else ''} {rental.status} REQ-{rental.pk}".lower(),
        })
    for payment in payments[:100]:
        payment_rental = payment.rental
        activities.append({
            "type": "payment",
            "date": payment.payment_date,
            "details": f"{payment_rental.name if payment_rental else 'Customer unavailable'} · {payment.payment_method or 'Payment'} · {payment.status}",
            "reference": payment.receipt_number or f"PAY-{payment.pk}",
            "amount": payment.amount_paid,
            "search": f"{payment_rental.name if payment_rental else ''} {payment.receipt_number or ''} {payment.status}".lower(),
        })
    for expense in expenses[:100]:
        activities.append({
            "type": "expense",
            "date": expense.expense_date,
            "details": f"{expense.description} · {expense.notes or 'Expense'}",
            "reference": expense.created_by.username if expense.created_by else "—",
            "amount": expense.amount,
            "search": f"{expense.description} {expense.notes or ''}".lower(),
        })
    activities.sort(key=lambda activity: activity["date"], reverse=True)

    return render(request, "admin/reports.html", {
        **report_data,
        "revenue": revenue,
        "total_expenses": total_expenses,
        "net_profit": revenue - total_expenses,
        "total_rentals": rental_summary["rental_count"],
        "total_requests": rental_summary["rental_count"],
        "payment_count": payments.count(),
        "expense_count": expense_summary["expense_count"],
        "chart_financial": chart_financial,
        "chart_activity": chart_activity,
        "has_financial_chart_data": bool(revenue_by_day or expense_by_day),
        "has_activity_chart_data": bool(rental_by_day or payments_by_day),
        "activities": activities,
    })


@login_required
def expense_entry(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    return render(request, "admin/add_expense.html", {
        'now': timezone.now(),
    })


@login_required
def reception_reports(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    now = timezone.localtime()
    selected_date = request.GET.get('date', '').strip()
    selected_month = request.GET.get('month', '').strip()
    selected_recent_month = request.GET.get('recent_month', '').strip()
    selected_period = request.GET.get('period', 'all').strip().lower()
    month_range = request.GET.get('month_range', '').strip().lower()

    def get_month_range(year, month):
        start_date = datetime(year, month, 1, 0, 0, 0)
        last_day = calendar.monthrange(year, month)[1]
        end_date = datetime(year, month, last_day, 23, 59, 59, 999999)
        tz = timezone.get_current_timezone()
        return timezone.make_aware(start_date, tz), timezone.make_aware(end_date, tz)

    def get_month_range_12_25(year, month):
        """Get date range from 12th to 25th of a month"""
        start_date = datetime(year, month, 12, 0, 0, 0)
        end_date = datetime(year, month, 25, 23, 59, 59, 999999)
        tz = timezone.get_current_timezone()
        return timezone.make_aware(start_date, tz), timezone.make_aware(end_date, tz)

    filter_start = None
    filter_end = None
    date_range_error = None
    if selected_date:
        parsed_date, date_range_error = parse_historical_date(selected_date)
        if parsed_date:
            filter_start = timezone.make_aware(datetime.combine(parsed_date, time.min), timezone.get_current_timezone())
            filter_end = timezone.make_aware(datetime.combine(parsed_date, time.max), timezone.get_current_timezone())
    elif selected_month:
        try:
            year_str, month_str = selected_month.split('-')
            year = int(year_str)
            month = int(month_str)
            if (year, month) > (now.year, now.month):
                date_range_error = "Report month cannot be in the future."
                selected_month = ''
            else:
                filter_start, filter_end = get_month_range(year, month)
                if (year, month) == (now.year, now.month):
                    filter_end = min(filter_end, now)
        except (ValueError, TypeError):
            selected_month = ''
            date_range_error = "Enter a valid report month."
    elif selected_recent_month:
        try:
            recent_months = int(selected_recent_month)
            if recent_months < 1:
                raise ValueError
            year = now.year
            month = now.month - recent_months + 1
            while month <= 0:
                month += 12
                year -= 1
            filter_start, _ = get_month_range(year, month)
            filter_end = now
        except (ValueError, TypeError):
            selected_recent_month = ''
            date_range_error = "Enter a valid recent-month range."
    elif month_range == 'current':
        # Filter 12th-25th of current month
        filter_start, filter_end = get_month_range_12_25(now.year, now.month)
        filter_end = min(filter_end, now)
    elif month_range == 'last':
        # Filter 12th-25th of last month
        year = now.year
        month = now.month - 1
        if month <= 0:
            month = 12
            year -= 1
        filter_start, filter_end = get_month_range_12_25(year, month)
    elif selected_period in ['daily', 'weekly', 'monthly', 'yearly']:
        if selected_period == 'daily':
            filter_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            filter_end = now
        elif selected_period == 'weekly':
            filter_start = now - timedelta(days=7)
            filter_end = now
        elif selected_period == 'monthly':
            filter_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            filter_end = now
        elif selected_period == 'yearly':
            filter_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            filter_end = now

    today = now.date()

    rentals_qs = SuitRequest.objects.select_related('suit')
    payments_qs = Payment.objects.select_related('rental')
    expenses_qs = Expense.objects.all()
    if filter_start and filter_end:
        rentals_qs = rentals_qs.filter(request_date__range=(filter_start, filter_end))
        payments_qs = payments_qs.filter(payment_date__range=(filter_start, filter_end))
        expenses_qs = expenses_qs.filter(expense_date__range=(filter_start, filter_end))
    elif date_range_error:
        rentals_qs = rentals_qs.none()
        payments_qs = payments_qs.none()
        expenses_qs = expenses_qs.none()

    total_suits = Suit.objects.count()
    available_quantity = Suit.objects.aggregate(total=Sum('quantity'))['total'] or 0
    rented_quantity = rentals_qs.filter(status="Active").aggregate(total=Sum('quantity_requested'))['total'] or 0
    total_requests = rentals_qs.count()
    pending_requests = rentals_qs.filter(status="Pending").count()
    active_requests = rentals_qs.filter(status="Active").count()
    returned_requests = rentals_qs.filter(status="Returned").count()
    revenue = rentals_qs.filter(status__in=["Active", "Returned"]).aggregate(total=Sum('total_amount'))['total'] or 0

    today_requests = rentals_qs.filter(request_date__date=today)
    today_new_rentals = today_requests.count()
    today_active_rentals = today_requests.filter(status="Active").count()
    today_returns = rentals_qs.filter(status="Returned", return_date__date=today).count()
    today_revenue = payments_qs.filter(payment_date__date=today).aggregate(total=Sum('amount_paid'))['total'] or 0
    today_expenses = expenses_qs.filter(expense_date__date=today).aggregate(total=Sum('amount'))['total'] or 0
    today_net = (today_revenue or 0) - (today_expenses or 0)

    overdue_rentals = rentals_qs.filter(status="Active").select_related("suit").order_by("due_date", "request_date")
    overdue_rentals = [req for req in overdue_rentals if req.is_overdue]

    recent_activity = []
    recent_activity.extend([
        {
            "type": "Rental",
            "title": f"{req.name} • {req.suit.suit_name if req.suit else 'Suit'}",
            "detail": f"Status: {req.status} • Qty: {req.quantity_requested}",
            "amount": req.total_amount,
            "date": req.request_date,
            "badge": "info",
        }
        for req in rentals_qs.order_by("-request_date")[:8]
    ])
    recent_activity.extend([
        {
            "type": "Payment",
            "title": f"Payment received • {payment.receipt_number or 'Receipt'}",
            "detail": f"{payment.rental.name if payment.rental else 'Rental'} • {payment.payment_method or 'Cash'}",
            "amount": payment.amount_paid,
            "date": payment.payment_date,
            "badge": "success",
        }
        for payment in payments_qs.order_by("-payment_date")[:6]
    ])
    recent_activity.extend([
        {
            "type": "Expense",
            "title": f"Expense • {expense.description[:40]}",
            "detail": expense.notes or "Operational cost",
            "amount": expense.amount,
            "date": expense.expense_date,
            "badge": "danger",
        }
        for expense in expenses_qs.order_by("-expense_date")[:6]
    ])
    recent_activity = sorted(recent_activity, key=lambda item: item["date"], reverse=True)[:10]

    context = {
        'total_suits': total_suits,
        'available_quantity': available_quantity,
        'rented_quantity': rented_quantity,
        'total_requests': total_requests,
        'pending_requests': pending_requests,
        'active_requests': active_requests,
        'returned_requests': returned_requests,
        'revenue': revenue,
        'selected_date': selected_date,
        'selected_month': selected_month,
        'selected_recent_month': selected_recent_month,
        'selected_period': selected_period,
        'month_range': month_range,
        'now': timezone.now(),
        'today': today,
        'today_requests': today_requests.order_by('-request_date')[:10],
        'today_new_rentals': today_new_rentals,
        'today_active_rentals': today_active_rentals,
        'today_returns': today_returns,
        'today_revenue': today_revenue,
        'today_expenses': today_expenses,
        'today_net': today_net,
        'overdue_rentals': overdue_rentals[:6],
        'recent_activity': recent_activity,
        'recent_requests': rentals_qs.filter(status__in=["Pending", "Active"]).order_by('-request_date')[:10],
    }
    return render(request, "reception/reception_reports.html", context)


@login_required
def admin_payments(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    # Date range parameters are restricted to historical dates.
    from_date_str = request.GET.get('from_date', '').strip()
    to_date_str = request.GET.get('to_date', '').strip()
    date_range_error = None
    
    filter_start = None
    filter_end = None
    
    # Parse date range if provided
    if from_date_str or to_date_str:
        parsed_from, from_error = parse_historical_date(from_date_str, "start date")
        parsed_to, to_error = parse_historical_date(to_date_str, "end date")
        date_range_error = from_error or to_error
        if parsed_from:
            filter_start = timezone.make_aware(datetime.combine(parsed_from, time.min), timezone.get_current_timezone())
        if parsed_to:
            filter_end = timezone.make_aware(datetime.combine(parsed_to, time.max), timezone.get_current_timezone())
        if filter_start and filter_end and filter_start > filter_end:
            date_range_error = "From date must be earlier than or equal to To date."
        if date_range_error:
            filter_start = filter_end = None

    payments = Payment.objects.select_related("cashier").order_by("-payment_date")
    expenses = Expense.objects.order_by("-expense_date")
    
    # Apply date filters
    if filter_start and filter_end:
        payments = payments.filter(payment_date__range=(filter_start, filter_end))
        expenses = expenses.filter(expense_date__range=(filter_start, filter_end))
    elif filter_start:
        payments = payments.filter(payment_date__gte=filter_start)
        expenses = expenses.filter(expense_date__gte=filter_start)
    elif filter_end:
        payments = payments.filter(payment_date__lte=filter_end)
        expenses = expenses.filter(expense_date__lte=filter_end)
    elif date_range_error:
        payments = payments.none()
        expenses = expenses.none()

    revenue_agg = payments.aggregate(total=Sum('amount_paid'))
    total_revenue = revenue_agg.get('total') or Decimal('0')

    total_expenses = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    dry_cleaning_qs = DryCleaning.objects.filter(paid=True)
    if filter_start and filter_end:
        dry_cleaning_qs = dry_cleaning_qs.filter(date__range=(filter_start.date(), filter_end.date()))
    elif filter_start:
        dry_cleaning_qs = dry_cleaning_qs.filter(date__gte=filter_start.date())
    elif filter_end:
        dry_cleaning_qs = dry_cleaning_qs.filter(date__lte=filter_end.date())
    elif date_range_error:
        dry_cleaning_qs = dry_cleaning_qs.none()
    dry_cleaning_expenses = dry_cleaning_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    other_expenses = total_expenses - dry_cleaning_expenses
    net_profit = total_revenue - total_expenses
    net_profit_before_cleaning = total_revenue - other_expenses

    today = timezone.localdate()
    today_income = payments.filter(payment_date__date=today).aggregate(total=Sum('amount_paid'))['total'] or 0
    today_expenses = expenses.filter(expense_date__date=today).aggregate(total=Sum('amount'))['total'] or 0
    today_profit = today_income - today_expenses
    today_transactions_count = payments.filter(payment_date__date=today).count()

    context = {
        "payments": payments,
        "expenses": expenses,
        "from_date": from_date_str,
        "to_date": to_date_str,
        "date_range_error": date_range_error,
        "payment_count": payments.count(),
        "expense_count": expenses.count(),
        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "net_revenue": (total_revenue - total_expenses),
        "today_income": today_income,
        "today_expenses": today_expenses,
        "today_profit": today_profit,
        "today_transactions_count": today_transactions_count,
        "active_count": SuitRequest.objects.filter(status="Active").count(),
        "returned_count": SuitRequest.objects.filter(status="Returned").count(),
        "page_type": "admin",
    }

    return render(request, "admin/admin_payments.html", context)


@login_required
def add_expense(request):
    if not is_reception_or_admin(request.user):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'Access denied. Reception/Admin only.'}, status=403)
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method != "POST":
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Invalid request method.'}, status=405)
        return redirect("reports")

    amount = request.POST.get("amount")
    category = request.POST.get("category", "Other")
    date_str = request.POST.get("expense_date") or request.POST.get("date")
    description = request.POST.get("description", "").strip()
    assigned_user_id = request.POST.get("assigned_user")
    assigned_staff_id = request.POST.get("assigned_staff")

    if not amount or not description:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Please enter expense amount and description.'}, status=400)
        messages.error(request, "Please enter expense amount and description.")
        return redirect("reports")

    expense_date_value, expense_date_error = parse_historical_date(date_str, "expense date")
    if expense_date_error:
        if is_ajax:
            return JsonResponse({'success': False, 'message': expense_date_error}, status=400)
        messages.error(request, expense_date_error)
        return redirect("reports")

    try:
        amount_value = Decimal(amount)
    except (TypeError, InvalidOperation):
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Please enter a valid expense amount.'}, status=400)
        messages.error(request, "Please enter a valid expense amount.")
        return redirect("reports")

    if amount_value <= 0:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Expense amount must be greater than zero.'}, status=400)
        messages.error(request, "Expense amount must be greater than zero.")
        return redirect("reports")

    expense_date = timezone.now()
    if expense_date_value:
        expense_date = timezone.make_aware(
            datetime.combine(expense_date_value, datetime.min.time()),
            timezone.get_current_timezone(),
        )

    creator = request.user
    if assigned_staff_id:
        try:
            assigned_staff = Staff.objects.get(id=assigned_staff_id)
            if assigned_staff.user:
                creator = assigned_staff.user
        except Staff.DoesNotExist:
            pass
    elif assigned_user_id:
        try:
            creator = User.objects.get(id=assigned_user_id)
        except User.DoesNotExist:
            creator = request.user

    expense = Expense.objects.create(
        description=description,
        amount=amount_value,
        expense_date=expense_date,
        created_by=creator,
        notes=category,
    )

    if category.lower() in ['salaries', 'salary'] and assigned_staff_id:
        try:
            staff = Staff.objects.get(id=assigned_staff_id)
            Payroll.objects.create(
                staff=staff,
                payment_date=expense_date,
                amount=amount_value,
                payment_type='Salary',
                month=expense_date.month,
                year=expense_date.year,
                created_by=request.user,
            )
        except Staff.DoesNotExist:
            pass

    if is_ajax:
        return JsonResponse({'success': True, 'message': 'Business expense recorded successfully.'})

    messages.success(request, "Business expense recorded successfully.")
    if is_reception_or_admin(request.user):
        return redirect("reports")
    return redirect("reports")


@login_required
def reception_payments(request):
    if not is_reception_or_cashier_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Cashier/Admin only.")
        return redirect("home")

    payments = Payment.objects.select_related(
        "cashier"
    ).order_by("-payment_date")

    expenses = Expense.objects.order_by("-expense_date")

    selected_date_raw = request.GET.get("date", "").strip()
    try:
        selected_date = parse_date(selected_date_raw) if selected_date_raw else timezone.localdate()
    except (TypeError, ValueError, ValidationError):
        selected_date = timezone.localdate()

    daily_payments = payments.filter(payment_date__date=selected_date)
    daily_expenses = expenses.filter(expense_date__date=selected_date)

    # Use DB aggregates to compute totals efficiently
    payment_agg = daily_payments.aggregate(total_revenue=Sum('amount_paid'))
    expense_agg = daily_expenses.aggregate(total_expenses=Sum('amount'))

    total_revenue = payment_agg.get('total_revenue') or 0
    total_expenses = expense_agg.get('total_expenses') or 0
    daily_profit = total_revenue - total_expenses
    daily_payment_count = daily_payments.count()
    daily_expense_count = daily_expenses.count()

    pending_reservations = SuitRequest.objects.filter(status="Pending").select_related('suit').order_by('-request_date')
    due_payments = SuitRequest.objects.filter(
        is_paid=False,
        status__in=["Approved", "Active", "Returned"]
    ).select_related('suit').order_by('-request_date')

    due_balance_total = sum((req.balance_due for req in due_payments), Decimal('0.00'))

    context = {
        "payments": payments,
        "expenses": expenses,

        "payment_count": payments.count(),
        "expense_count": expenses.count(),

        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "net_revenue": (total_revenue - total_expenses),

        "active_count": SuitRequest.objects.filter(status="Active").count(),
        "returned_count": SuitRequest.objects.filter(status="Returned").count(),

        "pending_reservations": pending_reservations,
        "pending_reservation_count": pending_reservations.count(),
        "due_payments": due_payments,
        "due_balance_count": due_payments.count(),
        "due_balance_total": due_balance_total,

        "page_type": "reception",
    }

    # Provide a short list of recent/eligible rentals for quick cash-box payments
    open_rentals = SuitRequest.objects.filter(status__in=["Approved", "Active", "Returned"]).order_by('-request_date')[:60]
    context['open_rentals'] = open_rentals

    return render(request,
                  "reception/reception_payments.html",
                  context)


@login_required
def mark_payment_as_paid(request, payment_id):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    payment = get_object_or_404(Payment, id=payment_id)
    # If POST, record a payment amount and date provided by the user.
    # detect AJAX
    is_ajax = False
    try:
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    except Exception:
        is_ajax = request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'

    if request.method == "POST":
        amount_raw = request.POST.get('amount', '0').strip() or '0'
        payment_date_raw = request.POST.get('payment_date', '').strip()
        try:
            amount = Decimal(amount_raw)
        except (InvalidOperation, TypeError, ValueError):
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Please enter a valid payment amount.'}, status=400)
            messages.error(request, "Please enter a valid payment amount.")
            return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')

        if amount <= 0:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Payment amount must be greater than zero.'}, status=400)
            messages.error(request, "Payment amount must be greater than zero.")
            return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')

        # Do not allow paying more than the balance due
        balance = payment.rental.balance_due or Decimal('0.00')
        if amount > balance:
            if is_ajax:
                return JsonResponse({'success': False, 'message': f'Amount exceeds remaining balance (${balance}).'}, status=400)
            messages.error(request, f"Amount exceeds remaining balance (${balance}).")
            return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')

        payment_method = request.POST.get('payment_method', '').strip() or (payment.payment_method or 'Cash')

        # Parse payment date (date input expected)
        if payment_date_raw:
            parsed_date, date_error = parse_historical_date(payment_date_raw, "payment date")
            if date_error:
                if is_ajax:
                    return JsonResponse({'success': False, 'message': date_error}, status=400)
                messages.error(request, date_error)
                return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')
            payment_dt = timezone.make_aware(
                datetime.combine(parsed_date, time.min),
                timezone.get_current_timezone(),
            )
        else:
            payment_dt = timezone.now()

        try:
            payment_date_only = timezone.localtime(payment_dt).date()
        except Exception:
            messages.error(request, "Invalid payment date.")
            return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')

        # Create a new Payment record for the remaining amount or partial
        payment_status = 'paid' if amount >= balance else 'partial'
        new_payment = Payment.objects.create(
            rental=payment.rental,
            cashier=request.user,
            amount_paid=amount,
            payment_method=payment_method,
            payment_reference=payment.payment_reference or None,
            status=payment_status,
            payment_date=payment_dt,
        )
        new_payment.receipt_number = f"RCPT-{new_payment.id:06d}"
        new_payment.save(update_fields=['receipt_number'])

        # If this covers the balance, mark rental as paid
        if amount >= balance:
            rental = payment.rental
            rental.is_paid = True
            rental.save(update_fields=['is_paid'])

        # Respond differently for AJAX requests so the frontend can show a popup
        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': f'Payment recorded (#{new_payment.id}).',
                'payment_id': new_payment.id,
                'receipt': new_payment.receipt_number,
            })

        messages.success(request, f"Payment recorded (#{new_payment.id}).")
        return redirect(request.META.get('HTTP_REFERER') or 'admin_payments')

    # Non-POST fallback
    # Non-POST fallback
    if is_ajax:
        return JsonResponse({'success': False, 'message': f'Use the payment form to record an amount for Payment #{payment.id}.'}, status=400)
    messages.info(request, f"Use the payment form to record an amount for Payment #{payment.id}.")
    return redirect("admin_payments")


@login_required
def record_payment(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        return redirect("admin_payments")

    rental_id = request.POST.get("rental_id")
    amount = request.POST.get("amount")
    method = request.POST.get("method")
    reference = request.POST.get("reference")

    if not rental_id:
        messages.error(request, "Please select a rental to record payment.")
        return redirect("admin_payments")

    req = get_object_or_404(SuitRequest, id=rental_id)

    if req.status not in ["Active", "Returned", "Approved"]:
        messages.error(request, "Selected rental is not eligible for manual payment.")
        return redirect("admin_payments")

    try:
        amount_value = Decimal(amount)
    except (TypeError, ValueError, InvalidOperation):
        messages.error(request, "Please enter a valid payment amount.")
        return redirect("admin_payments")

    if amount_value <= 0:
        messages.error(request, "Payment amount must be greater than zero.")
        return redirect("admin_payments")

    balance_due = req.balance_due
    if balance_due <= Decimal('0.00'):
        messages.error(request, "This rental is already fully paid.")
        return redirect("admin_payments")

    if amount_value > balance_due:
        messages.error(request, "Amount paid cannot exceed the remaining balance.")
        return redirect("admin_payments")

    req.payment_method = method.title() if method else req.payment_method or "Cash"
    if reference:
        req.payment_reference = reference

    payment = Payment.objects.create(
        rental=req,
        cashier=request.user,
        amount_paid=amount_value,
        payment_method=req.payment_method,
        payment_reference=reference,
        status='paid' if amount_value >= balance_due else 'partial',
        payment_date=timezone.now(),
    )
    payment.receipt_number = f"RCPT-{payment.id:06d}"
    payment.save(update_fields=['receipt_number'])

    if amount_value >= balance_due:
        req.is_paid = True
    req.save(update_fields=["is_paid", "payment_method", "payment_reference"])

    messages.success(request, "Payment recorded successfully.")
    return redirect("admin_payments")


@login_required
def payment_receipt(request, payment_id):
    payment = get_object_or_404(Payment, id=payment_id)
    req = payment.rental
    return render(request, "admin/receipt.html", {
        "req": req,
        "payment": payment,
        "company_name": "Suit Rental",
        "company_address": "123 Main Street, City",
        "company_phone": "+252 61 234 5678",
    })


@login_required
def dry_cleaning_page(request):
    """Admin review of reception laundry requests and active laundry jobs."""
    if not is_admin(request.user):
        messages.error(request, "Only an admin can approve laundry requests.")
        return redirect("dashboard" if is_reception(request.user) else "home")

    if request.method == 'POST':
        action = request.POST.get('action')
        cleaning_id = request.POST.get('cleaning_id')
        
        if not cleaning_id:
            messages.error(request, 'Laguma helin item-ka.')
            return redirect('dry_cleaning')
        
        dry_clean_record = get_object_or_404(DryCleaning, id=cleaning_id)

        if dry_clean_record.status != 'pending':
            messages.error(request, "This laundry request has already been reviewed.")
            return redirect('dry_cleaning')
        
        if action in ['approve_request', 'approve_payment']:
            now = timezone.now()
            dry_clean_record.status = 'sent_to_laundry'
            dry_clean_record.approved_by = request.user
            dry_clean_record.approved_at = now
            dry_clean_record.sent_to_laundry_at = now
            dry_clean_record.save(update_fields=['status', 'approved_by', 'approved_at', 'sent_to_laundry_at'])
            dry_clean_record.suit.status = 'Dry Cleaning'
            dry_clean_record.suit.save(update_fields=['status'])
            messages.success(request, f"'{dry_clean_record.suit.suit_name}' approved and sent to laundry.")

        elif action in ['reject_request', 'reject_payment']:
            dry_clean_record.status = 'rejected'
            dry_clean_record.paid = False
            dry_clean_record.save(update_fields=['status', 'paid'])
            messages.warning(request, f"✗ '{dry_clean_record.suit.suit_name}' has been rejected. It will stay in the rejected section and cannot be resubmitted.")
        
        return redirect('dry_cleaning')
    
    # Get all DryCleaning records by status
    pending_cleanings = DryCleaning.objects.filter(status='pending').select_related('suit').order_by('-created_at')
    approved_cleanings = DryCleaning.objects.filter(
        status__in=['approved', 'sent_to_laundry', 'in_laundry', 'completed']
    ).select_related('suit', 'requested_by', 'approved_by').order_by('-created_at')
    rejected_cleanings = DryCleaning.objects.filter(status='rejected').select_related('suit').order_by('-created_at')
    
    context = {
        'laundry_items': pending_cleanings,  # Items awaiting admin review
        'approved_cleanings': approved_cleanings,  # Admin approved items
        'rejected_cleanings': rejected_cleanings,  # Admin rejected items
        'pending_count': pending_cleanings.count(),
        'approved_count': approved_cleanings.count(),
        'rejected_count': rejected_cleanings.count(),
        'total_pending_amount': sum(c.amount for c in pending_cleanings),
        'active_cleanings': approved_cleanings,
    }
    return render(request, 'admin/dry_cleaning.html', context)


@login_required
def mark_dry_cleaning_paid(request, entry_id):
    if not is_admin(request.user):
        messages.error(request, "Only an admin can confirm laundry payments.")
        return redirect('dashboard' if is_reception(request.user) else 'home')

    if request.method != 'POST':
        return redirect('dry_cleaning')

    entry = get_object_or_404(DryCleaning, id=entry_id)
    entry.paid = True
    entry.save(update_fields=['paid'])
    messages.success(request, f"'{entry.suit.suit_name}' dry cleaning was marked as paid.")
    return redirect('dry_cleaning')


@login_required
def dry_cleaning(request):
    """Render the Dry Cleaning operational queue page with live DB data.

    Shows recent `SuitRequest` objects that are `Returned` and awaiting
    cleaning/processing. "Mark as Ready" action is handled by
    `dry_cleaning_ledger` which records an `Expense` and marks the suit
    `Available`.
    """
    returned_requests = SuitRequest.objects.filter(status="Returned").select_related('suit').order_by('-return_date')

    context = {
        'returned_requests': returned_requests,
        'total_items': returned_requests.count(),
    }
    return render(request, "reception/dry_cleaning.html", context)


@login_required
def dry_cleaning_ledger(request):
    """Handle ledger display and recording cleaning expenses.

    POST expects `suit_id`, `cleaning_cost`, and optional `laundry_notes`.
    Creates an `Expense`, sets the `Suit.status` to `Available`, and
    redirects back to the dry cleaning page.
    """
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect('home')

    if request.method == 'POST':
        suit_id = request.POST.get('suit_id')
        cleaning_cost = request.POST.get('cleaning_cost')
        laundry_notes = request.POST.get('laundry_notes', '')

        if not suit_id or not cleaning_cost:
            messages.error(request, "Missing suit or cleaning cost.")
            return redirect('dry_cleaning')

        try:
            suit = Suit.objects.get(id=int(suit_id))
        except (Suit.DoesNotExist, ValueError):
            messages.error(request, "Selected suit not found.")
            return redirect('dry_cleaning')

        try:
            amount = Decimal(cleaning_cost)
        except (InvalidOperation, TypeError):
            messages.error(request, "Enter a valid cleaning cost amount.")
            return redirect('dry_cleaning')

        dry_clean = DryCleaning.objects.filter(suit=suit).order_by('-created_at').first()
        if suit.status != 'Dry Cleaning' or not dry_clean or dry_clean.status not in ['completed', 'approved']:
            messages.error(request, "The laundry job must be completed before recording its return and expense.")
            return redirect('dry_cleaning')

        with transaction.atomic():
            suit = Suit.objects.select_for_update().get(pk=suit_id)
            dry_clean = DryCleaning.objects.select_for_update().get(pk=dry_clean.pk)
            if suit.status != "Dry Cleaning" or dry_clean.status not in ["completed", "approved"]:
                messages.error(request, "This cleaning job has already been returned or is not complete.")
                return redirect("dry_cleaning")

            quantity_delta = dry_clean.rental_request.quantity_requested if dry_clean.rental_request_id else 0
            Expense.objects.create(
                description=f"Cleaning for {suit.suit_name} (#{suit.id})",
                amount=amount,
                created_by=request.user,
                notes=laundry_notes,
            )

            suit.quantity += quantity_delta
            suit.status = 'Available'
            suit.save(update_fields=['quantity', 'status'])
            record_inventory_movement(
                suit,
                "cleaning_completed",
                quantity_delta=quantity_delta,
                performed_by=request.user,
                rental_request=dry_clean.rental_request,
                notes="Cleaning expense recorded and items returned to inventory.",
            )
            dry_clean.status = 'returned'
            dry_clean.returned_at = timezone.now()
            dry_clean.paid = True
            dry_clean.save(update_fields=['status', 'returned_at', 'paid'])

        messages.success(request, f"Recorded cleaning expense and marked '{suit.suit_name}' as Available.")
        return redirect('dry_cleaning')

    # GET: show ledger with real dry cleaning records and related expenses
    from django.utils import timezone as _tz
    now = _tz.now()

    dry_cleaning_records = DryCleaning.objects.select_related('suit').order_by('-created_at')
    this_month_records = dry_cleaning_records.filter(date__year=now.year, date__month=now.month)
    cleaning_expenses = []
    for record in this_month_records:
        cleaning_expenses.append({
            'id': record.id,
            'description': f"Dry cleaning for {record.suit.suit_name}",
            'notes': record.notes or 'Dry cleaning charge',
            'expense_date': record.date,
            'amount': record.amount,
            'status': record.status,
            'paid': record.paid,
            'suit': record.suit,
        })

    month_total = this_month_records.aggregate(total=Sum('amount'))['total'] or 0
    returned_requests = SuitRequest.objects.filter(
        status='Returned',
        suit__isnull=False,
    ).select_related('suit').order_by('-return_date')
    pending_dry_cleaning_queue = returned_requests[:8]

    context = {
        'cleaning_expenses': cleaning_expenses,
        'month_total': month_total,
        'returned_requests': returned_requests,
        'dry_cleaning_queue': pending_dry_cleaning_queue,
        'pending_dry_cleaning_count': pending_dry_cleaning_queue.count() if hasattr(pending_dry_cleaning_queue, 'count') else len(pending_dry_cleaning_queue),
        'inventory_total': Suit.objects.count(),
        'dry_cleaning_records': this_month_records,
    }
    return render(request, "admin/dry_cleaning_ledger.html", context)


@login_required
def laundry_backlog_view(request):
    """Reception requests laundry; staff can advance approved jobs to return."""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect('home')

    if request.method == 'POST':
        action = request.POST.get('action', 'mark_paid')
        request_id = request.POST.get('request_id')
        cleaning_cost = request.POST.get('cleaning_cost', '0')
        laundry_notes = request.POST.get('laundry_notes', '')

        if action in ['start_laundry', 'complete_laundry']:
            dry_clean = get_object_or_404(DryCleaning, id=request.POST.get('cleaning_id'))
            expected_status = 'sent_to_laundry' if action == 'start_laundry' else 'in_laundry'
            next_status = 'in_laundry' if action == 'start_laundry' else 'completed'
            if dry_clean.status != expected_status:
                messages.error(request, "This laundry job has already changed status.")
                return redirect('laundry_backlog')
            dry_clean.status = next_status
            update_fields = ['status']
            if next_status == 'completed':
                dry_clean.completed_at = timezone.now()
                update_fields.append('completed_at')
            dry_clean.save(update_fields=update_fields)
            messages.success(request, f"'{dry_clean.suit.suit_name}' is now {dry_clean.get_status_display().lower()}.")
            return redirect('laundry_backlog')

        if not request_id:
            messages.error(request, "Ma jiro codsi dhar-dhaqin la tilmaamay.")
            return redirect('laundry_backlog')

        suit_request = SuitRequest.objects.filter(
            id=request_id,
            status='Returned'
        ).select_related('suit').first()

        if not suit_request:
            messages.error(request, "Cillad baa dhacday: Suudhkaan lagama helin liiska dhar-dhaqista.")
            return redirect('laundry_backlog')
        if not suit_request.suit_id:
            messages.error(request, "Accessory-only rentals do not require suit laundry.")
            return redirect('laundry_backlog')

        if action in ['submit_request', 'mark_paid']:
            try:
                amount = Decimal(cleaning_cost)
            except (InvalidOperation, TypeError, ValueError):
                messages.error(request, "Fadlan geli lacag sax ah oo nadiifinta ah.")
                return redirect('laundry_backlog')

            existing = DryCleaning.objects.filter(
                suit_id=suit_request.suit.id,
                status__in=['pending', 'approved', 'sent_to_laundry', 'in_laundry', 'completed'],
            ).order_by('-created_at').first()
            if existing:
                messages.info(request, f"'{suit_request.suit.suit_name}' is already in the queue.")
                return redirect('laundry_backlog')

            admin_sent = is_admin(request.user)
            now = timezone.now()
            DryCleaning.objects.create(
                suit_id=suit_request.suit.id,
                rental_request=suit_request,
                requested_by=request.user,
                approved_by=request.user if admin_sent else None,
                amount=amount,
                paid=False,
                status='sent_to_laundry' if admin_sent else 'pending',
                notes=laundry_notes or ("Sent directly by admin" if admin_sent else "Pending admin laundry approval"),
                approved_at=now if admin_sent else None,
                sent_to_laundry_at=now if admin_sent else None,
            )
            if admin_sent:
                suit_request.suit.status = 'Dry Cleaning'
                suit_request.suit.save(update_fields=['status'])
                messages.success(request, f"'{suit_request.suit.suit_name}' sent directly to laundry.")
            else:
                messages.success(request, f"'{suit_request.suit.suit_name}' laundry request submitted for admin approval.")
            return redirect('laundry_backlog')

        elif action == 'mark_ready':
            dry_clean = DryCleaning.objects.filter(
                rental_request=suit_request,
                status__in=['completed', 'approved'],
            ).order_by('-created_at').first()
            if not dry_clean:
                dry_clean = DryCleaning.objects.filter(
                    suit=suit_request.suit,
                    rental_request__isnull=True,
                    status='approved',
                ).order_by('-created_at').first()

            if not dry_clean:
                messages.error(request, f"'{suit_request.suit.suit_name}' must be completed by laundry before returning to inventory.")
                return redirect('laundry_backlog')

            with transaction.atomic():
                suit_request = SuitRequest.objects.select_for_update().get(pk=suit_request.pk)
                suit = Suit.objects.select_for_update().get(pk=suit_request.suit_id)
                dry_clean = DryCleaning.objects.select_for_update().get(pk=dry_clean.pk)
                if (
                    suit_request.status != "Returned"
                    or suit.status != "Dry Cleaning"
                    or dry_clean.status not in {"completed", "approved"}
                ):
                    messages.error(request, "This cleaning job has already been returned or is not complete.")
                    return redirect("laundry_backlog")

                try:
                    amount = Decimal(cleaning_cost) if cleaning_cost not in [None, ''] else dry_clean.amount
                except (InvalidOperation, TypeError, ValueError):
                    amount = dry_clean.amount

                expense_exists = Expense.objects.filter(
                    description__icontains=f"Dry cleaning for {suit.suit_name}"
                ).exists()

                if not expense_exists:
                    Expense.objects.create(
                        description=f"Dry cleaning for {suit.suit_name} (Tag: #{suit.id}). {laundry_notes}",
                        amount=amount,
                        created_by=request.user,
                        notes=laundry_notes or f"Dry cleaning cost for {suit.suit_name}",
                    )

                quantity_delta = suit_request.quantity_requested or 0
                suit.quantity += quantity_delta
                suit.status = 'Available'
                suit.save(update_fields=['quantity', 'status'])
                record_inventory_movement(
                    suit,
                    "cleaning_completed",
                    quantity_delta=quantity_delta,
                    performed_by=request.user,
                    rental_request=suit_request,
                    notes="Cleaning completed; items returned to available inventory.",
                )

                suit_request.status = 'Returned'
                suit_request.save(update_fields=['status'])

                dry_clean.paid = True
                dry_clean.status = 'returned'
                dry_clean.returned_at = timezone.now()
                dry_clean.save(update_fields=['paid', 'status', 'returned_at'])

                messages.success(request, f"🧼 {suit.suit_name} waa laga soo saaray dhar-dhaqista, kharashkiisana waa la diiwaangeliyey!")

            return redirect('laundry_backlog')

    active_tab = request.GET.get('tab', 'all').lower()
    all_items = SuitRequest.objects.filter(
        status='Returned',
        suit__isnull=False,
    ).select_related('suit').prefetch_related('suit__dry_cleanings')

    filtered_items = []
    for item in all_items:
        suit = item.suit
        if suit.status == 'Available':
            continue

        dry_clean = suit.dry_cleanings.order_by('-created_at').first()
        if not dry_clean:
            filtered_items.append(item)
            continue
        if dry_clean.status in ['rejected', 'returned']:
            continue
        if dry_clean.status in ['sent_to_laundry', 'in_laundry', 'completed', 'approved']:
            if active_tab == 'pending':
                continue
            if active_tab == 'approved':
                filtered_items.append(item)
                continue
        if active_tab == 'pending' and dry_clean.status != 'pending':
            continue
        if active_tab == 'approved' and dry_clean.status not in ['sent_to_laundry', 'in_laundry', 'completed', 'approved']:
            continue
        filtered_items.append(item)

    page_number = request.GET.get('page', 1)
    paginator = Paginator(filtered_items, 10)
    page_obj = paginator.get_page(page_number)

    template_name = 'admin/loundary_admin.html' if is_admin(request.user) else 'reception/laundry_page.html'
    return render(request, template_name, {
        'laundry_items': page_obj.object_list,
        'page_obj': page_obj,
        'active_tab': active_tab,
        'is_admin_user': is_admin(request.user),
    })


@login_required
def apply_discount(request, rental_id=None):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        messages.error(request, "Discount must be submitted via POST.")
        return redirect("admin_payments")

    if rental_id is None:
        rental_id = request.POST.get("rental_id")

    if not rental_id:
        messages.error(request, "No rental was specified for discount.")
        return redirect("admin_payments")

    try:
        rental_id_int = int(rental_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid rental identifier provided.")
        return redirect("admin_payments")

    rental = SuitRequest.objects.filter(id=rental_id_int).first()
    if not rental:
        messages.error(request, f"Rental #{rental_id} was not found.")
        return redirect("admin_payments")

    discount_type = request.POST.get("discount_type", "fixed")
    discount_value = Decimal(request.POST.get("discount_value", "0"))
    discount_reason = request.POST.get("discount_reason", "")

    if discount_value <= 0:
        messages.error(request, "Discount value must be greater than zero.")
        return redirect("admin_payments")

    if discount_type == "percentage":
        if discount_value > 100:
            messages.error(request, "Percentage discount cannot exceed 100%.")
            return redirect("admin_payments")
        discount_amount = rental.total_amount * (discount_value / Decimal(100))
    else:
        if discount_value > rental.total_amount:
            messages.error(request, "Fixed discount cannot exceed total rental amount.")
            return redirect("admin_payments")
        discount_amount = discount_value

    final_total = rental.total_amount - discount_amount
    rental.total_amount = final_total
    rental.notes = (rental.notes or "") + f"\n[Discount: ${discount_amount:.2f} - {discount_reason}]"
    rental.save(update_fields=["total_amount", "notes"])

    Notification.objects.create(
        user=request.user,
        message=f"Discount of ${discount_amount:.2f} applied to rental #{rental_id} for {rental.name}"
    )

    messages.success(request, f"Discount of ${discount_amount:.2f} applied to rental #{rental_id}.")
    return redirect("admin_payments")


@login_required
def process_refund(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        messages.error(request, "Refund must be submitted via POST.")
        return redirect("admin_payments")

    payment_id = request.POST.get("payment_id")
    if not payment_id:
        messages.error(request, "No payment was specified for refund.")
        return redirect("admin_payments")

    # Validate payment_id is an integer and that the Payment exists.
    try:
        payment_id_int = int(payment_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid payment identifier provided.")
        return redirect("admin_payments")

    payment = Payment.objects.filter(id=payment_id_int).first()
    if not payment:
        messages.error(request, f"Payment #{payment_id} was not found.")
        return redirect("admin_payments")
    refund_amount = Decimal(request.POST.get("refund_amount", "0"))
    if refund_amount <= 0 or refund_amount > payment.amount_paid:
        messages.error(request, "Refund amount must be greater than zero and not exceed the original payment.")
        return redirect("admin_payments")

    payment.refund_amount = refund_amount
    payment.refund_method = request.POST.get("refund_method", payment.payment_method)
    payment.refund_date = timezone.now()
    payment.notes = request.POST.get("reason", payment.notes)
    payment.status = 'refunded' if refund_amount >= payment.amount_paid else 'partial_refund'
    payment.save(update_fields=['refund_amount', 'refund_method', 'refund_date', 'notes', 'status'])

    rental = payment.rental
    if refund_amount >= payment.amount_paid:
        rental.is_paid = False
    rental.save(update_fields=['is_paid'])

    messages.success(request, f"Refund recorded for payment #{payment_id}.")
    return redirect("admin_payments")


@login_required
def export_payments(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="payments.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'Customer', 'Phone', 'Suit', 'Amount', 'Paid', 'Method', 'Reference', 'Status', 'Request Date'])

    for req in SuitRequest.objects.order_by('-request_date'):
        writer.writerow([
            req.id,
            req.name,
            req.phone,
            req.suit.suit_name if req.suit_id else "Accessories only",
            req.total_amount,
            req.is_paid,
            req.payment_method,
            req.payment_reference,
            req.status,
            req.request_date,
        ])

    return response


@login_required
def download_report(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    report_data = _get_report_data(request)
    if report_data["date_range_error"]:
        return HttpResponse(
            f"Invalid report date range: {report_data['date_range_error']}",
            status=400,
            content_type="text/plain",
        )

    rentals = report_data["rentals"]
    payments = report_data["payments"]
    expenses = report_data["expenses"]
    revenue = rentals.filter(status__in=["Active", "Returned"]).aggregate(
        total=Sum("total_amount")
    )["total"] or Decimal("0.00")
    expense_summary = expenses.aggregate(total=Sum("amount"), expense_count=Count("id"))
    total_expenses = expense_summary["total"] or Decimal("0.00")
    total_rentals = rentals.aggregate(total=Count("id"))["total"]

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("\ufeff")
    response["Content-Disposition"] = (
        f'attachment; filename="report_{report_data["start_date"]}_to_{report_data["end_date"]}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["Summary"])
    writer.writerow(["Start date", report_data["start_date"]])
    writer.writerow(["End date", report_data["end_date"]])
    writer.writerow(["Total revenue", f"{revenue:.2f}"])
    writer.writerow(["Total expenses", f"{total_expenses:.2f}"])
    writer.writerow(["Net profit", f"{revenue - total_expenses:.2f}"])
    writer.writerow(["Number of rentals", total_rentals])
    writer.writerow([])
    writer.writerow(["Rentals/Payments"])
    writer.writerow(["Record type", "Date", "Customer", "Suit", "Reference", "Status", "Amount"])

    for rental in rentals:
        writer.writerow([
            "Rental",
            timezone.localtime(rental.request_date).strftime("%Y-%m-%d %H:%M"),
            rental.name,
            rental.suit.suit_name if rental.suit else "",
            f"REQ-{rental.pk}",
            rental.status,
            rental.total_amount,
        ])
    for payment in payments:
        rental = payment.rental
        writer.writerow([
            "Payment",
            timezone.localtime(payment.payment_date).strftime("%Y-%m-%d %H:%M"),
            rental.name if rental else "",
            rental.suit.suit_name if rental and rental.suit else "",
            payment.receipt_number or f"PAY-{payment.pk}",
            payment.status,
            payment.amount_paid,
        ])

    writer.writerow([])
    writer.writerow(["Expenses"])
    writer.writerow(["Date", "Description", "Amount", "Created by", "Notes"])
    for expense in expenses:
        writer.writerow([
            timezone.localtime(expense.expense_date).strftime("%Y-%m-%d %H:%M"),
            expense.description,
            expense.amount,
            expense.created_by.username if expense.created_by else "",
            expense.notes or "",
        ])

    return response


@login_required
def pending_balances(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    payments = SuitRequest.objects.filter(is_paid=False).order_by('-request_date')
    total_revenue = payments.aggregate(total=Sum('total_amount'))['total'] or 0
    payment_count = payments.count()

    context = {
        'payments': payments,
        'total_revenue': total_revenue,
        'payment_count': payment_count,
        'returned_count': 0,
        'active_count': payments.filter(status="Active").count(),
        'today_revenue': 0,
        'today_count': 0,
        'pending_balances': total_revenue,
        'pending_count': payment_count,
        'total_deposits': 0,
        'active_rentals': payments,
        'shift_id': f"{request.user.id:03d}",
        'shift_start': timezone.now(),
    }
    return render(request, "reception/reception_payments.html", context)


@login_required
def deposit_management(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    messages.info(request, "Deposit management is not yet implemented.")
    return redirect("admin_payments")


@login_required
def daily_report(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    messages.info(request, "Daily reporting is not yet implemented.")
    return redirect("admin_payments")


@login_required
def close_shift(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    messages.success(request, "Shift closed successfully.")
    return redirect("admin_payments")


# =========================
# SEND NOTIFICATIONS
# =========================
@login_required
def send_notifications(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method == "POST":
        message = request.POST.get("message")
        send_to = request.POST.get("send_to")

        users = []
        if send_to == "all":
            from django.contrib.auth.models import User
            users = User.objects.filter(is_active=True)
        elif send_to == "active":
            users = [req.user for req in SuitRequest.objects.filter(status="Active").distinct()]
        elif send_to == "overdue":
            users = [req.user for req in SuitRequest.objects.filter(status="Active") if req.is_overdue]

        for user in users:
            Notification.objects.create(
                user=user,
                message=message
            )

        messages.success(request, f"Notifications sent to {len(users)} users.")
        return redirect("dashboard")
    return render(request, "admin/send_notifications.html")


# =========================
# USER MANAGEMENT (ADMIN ONLY)
# =========================
@login_required
def manage_users(request):
    if not _can_manage_customer_rentals(request.user):
        messages.error(request, "Access denied. Admin or Reception only.")
        return redirect("home")
    
    from django.contrib.auth.models import User
    users = User.objects.filter(is_staff=False).order_by('-date_joined')
    for u in users:
        Profile.objects.get_or_create(user=u)
    
    current_q = request.GET.get("q", "").strip()
    if current_q:
        users = users.filter(
            Q(username__icontains=current_q) |
            Q(email__icontains=current_q)
        )
    
    total_users = users.count()
    customer_count = total_users
    admin_count = (
        User.objects.filter(is_staff=True)
        .exclude(profile__is_reception=True)
        .exclude(profile__is_cashier=True)
        .count()
        if is_admin(request.user)
        else 0
    )
    
    context = {
        "users": users,
        "current_q": current_q,
        "total_users": total_users,
        "admin_count": admin_count,
        "active_users": User.objects.filter(is_active=True, is_staff=False).count(),
        "customer_count": customer_count,
        "is_reception_user": is_reception(request.user),
        "hide_admin_sidebar": is_reception(request.user),
    }
    
    return render(request, "admin/manage_users.html", context)


@login_required
def edit_user(request, user_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    user = get_object_or_404(User, pk=user_id)
    profile, _ = Profile.objects.get_or_create(user=user)
    is_protected_admin = user.is_superuser or (
        user.is_staff and not profile.is_cashier and not profile.is_reception
    )
    can_edit_access = user.pk != request.user.pk and not is_protected_admin

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip()
        new_password = request.POST.get("new_password", "")
        password_confirmation = request.POST.get("password_confirmation", "")
        customer_phone = request.POST.get("customer_phone", "").strip()
        national_id = request.POST.get("national_id", "").strip()
        present_address = request.POST.get("present_address", "").strip()
        parents_address = request.POST.get("parents_address", "").strip()

        if not username:
            messages.error(request, "Username is required.")
            return redirect("edit_user", user_id=user.pk)

        try:
            User._meta.get_field("username").clean(username, user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("edit_user", user_id=user.pk)

        if customer_phone:
            try:
                customer_phone = validate_phone_number(customer_phone)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("edit_user", user_id=user.pk)
        try:
            national_id = validate_national_id(national_id)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("edit_user", user_id=user.pk)

        if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
            messages.error(request, "That username is already in use.")
            return redirect("edit_user", user_id=user.pk)

        if email:
            try:
                validate_email(email)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return redirect("edit_user", user_id=user.pk)

        if new_password or password_confirmation:
            if len(new_password) < 4:
                messages.error(request, "New password must be at least 4 characters long.")
                return redirect("edit_user", user_id=user.pk)
            if new_password != password_confirmation:
                messages.error(request, "Password confirmation does not match.")
                return redirect("edit_user", user_id=user.pk)

        profile_photo = request.FILES.get("profile_photo")
        id_document = request.FILES.get("id_document")
        try:
            if profile_photo:
                validate_image_upload(profile_photo, "profile photo")
            if id_document:
                validate_image_upload(id_document, "ID document")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("edit_user", user_id=user.pk)

        with transaction.atomic():
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            user.email = email
            if can_edit_access:
                role = request.POST.get("role", "")
                if role != "customer":
                    messages.error(request, "Choose a valid account role.")
                    return redirect("edit_user", user_id=user.pk)
                user.is_active = request.POST.get("is_active") == "true"
                user.is_staff = False
                profile.is_reception = False
                profile.is_cashier = False
            if new_password:
                user.set_password(new_password)
            if profile_photo:
                profile.profile_photo = profile_photo
            if id_document:
                profile.id_document = id_document
            profile.customer_phone = customer_phone
            profile.national_id = national_id
            profile.present_address = present_address
            profile.parents_address = parents_address
            user.save()
            profile.save()

        if new_password and user.pk == request.user.pk:
            update_session_auth_hash(request, user)
        messages.success(request, f"Profile for '{user.username}' updated successfully.")
        return redirect("manage_users")

    if is_protected_admin:
        role = "admin"
    elif profile.is_reception:
        role = "reception"
    elif profile.is_cashier:
        role = "cashier"
    else:
        role = "customer"

    return render(request, "admin/edit_user.html", {
        "managed_user": user,
        "profile": profile,
        "role": role,
        "is_protected_admin": is_protected_admin,
        "can_edit_access": can_edit_access,
    })


@login_required
def create_cashier(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")

        if not username:
            messages.error(request, "Username is required.")
            return redirect("manage_users")

        if password != password2:
            messages.error(request, "Passwords do not match.")
            return redirect("manage_users")

        if len(password) < 4:
            messages.error(request, "Password must be at least 4 characters long.")
            return redirect("manage_users")

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Username already exists.")
            return redirect("manage_users")

        user = User.objects.create_user(username=username, password=password, email=email)
        user.is_active = True
        user.is_staff = True
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.is_cashier = True
        profile.is_reception = False
        profile.save()

        messages.success(request, f"Cashier '{username}' created successfully.")
        return redirect("manage_users")

    return redirect("manage_users")


@login_required
def add_staff(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        position = request.POST.get("position", "").strip()
        phone = request.POST.get("phone", "").strip()
        hire_date_raw = request.POST.get("hire_date", "").strip()
        monthly_salary_raw = request.POST.get("monthly_salary", "").strip()
        status = request.POST.get("status", "active")
        create_login = bool(request.POST.get("create_login"))

        if not name:
            messages.error(request, "Staff member name is required.")
            return redirect("manage_users")

        if not monthly_salary_raw:
            messages.error(request, "Monthly salary is required.")
            return redirect("manage_users")

        try:
            monthly_salary = Decimal(monthly_salary_raw)
        except (InvalidOperation, TypeError):
            messages.error(request, "Please enter a valid salary amount.")
            return redirect("manage_users")

        if monthly_salary < 0:
            messages.error(request, "Salary cannot be negative.")
            return redirect("manage_users")

        hire_date, hire_date_error = parse_historical_date(hire_date_raw, "hire date")
        if hire_date_error:
            messages.error(request, hire_date_error)
            return redirect("manage_users")

        staff_user = None
        if create_login:
            username = request.POST.get("username", "").strip()
            email = request.POST.get("email", "").strip()
            password = request.POST.get("password", "")
            password2 = request.POST.get("password2", "")

            if not username:
                messages.error(request, "Username is required for staff login.")
                return redirect("manage_users")

            if password != password2:
                messages.error(request, "Passwords do not match.")
                return redirect("manage_users")

            if len(password) < 4:
                messages.error(request, "Password must be at least 4 characters long.")
                return redirect("manage_users")

            if User.objects.filter(username__iexact=username).exists():
                messages.error(request, "Username already exists.")
                return redirect("manage_users")

            staff_user = User.objects.create_user(username=username, password=password, email=email)
            staff_user.is_active = True
            staff_user.is_staff = True
            staff_user.save()
            Profile.objects.get_or_create(user=staff_user)

        Staff.objects.create(
            user=staff_user,
            name=name,
            position=position or None,
            phone=phone or None,
            monthly_salary=monthly_salary,
            hire_date=hire_date,
            status=status if status in dict(Staff.STATUS_CHOICES) else 'active',
        )

        messages.success(request, f"Staff member '{name}' created successfully.")
        return redirect("manage_users")

    return redirect("manage_users")


@login_required
def add_payroll(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method == "POST":
        staff_id = request.POST.get("staff")
        payment_type = request.POST.get("payment_type", "Salary").strip()
        amount_str = request.POST.get("amount", "").strip()
        payment_date_str = request.POST.get("payment_date", "").strip()
        month = request.POST.get("month")
        year = request.POST.get("year")
        notes = request.POST.get("notes", "").strip()

        if not staff_id or not amount_str or not payment_date_str:
            messages.error(request, "Staff, amount and payment date are required.")
            return redirect("manage_users")

        try:
            staff = Staff.objects.get(id=staff_id)
        except Staff.DoesNotExist:
            messages.error(request, "Selected staff member does not exist.")
            return redirect("manage_users")

        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, TypeError):
            messages.error(request, "Enter a valid payroll amount.")
            return redirect("manage_users")

        if amount <= 0:
            messages.error(request, "Payroll amount must be greater than zero.")
            return redirect("manage_users")

        payment_date, payment_date_error = parse_historical_date(payment_date_str, "payment date")
        if payment_date_error or not payment_date:
            messages.error(request, payment_date_error or "Enter a valid payment date.")
            return redirect("manage_users")

        try:
            month = int(month) if month else payment_date.month
        except (ValueError, TypeError):
            month = payment_date.month

        try:
            year = int(year) if year else payment_date.year
        except (ValueError, TypeError):
            year = payment_date.year

        Payroll.objects.create(
            staff=staff,
            payment_date=payment_date,
            amount=amount,
            payment_type=payment_type or "Salary",
            month=month,
            year=year,
            created_by=request.user,
            notes=notes,
        )

        messages.success(request, "Payroll entry added successfully.")
        return redirect("manage_users")

    return redirect("manage_users")


@login_required
def reception_form(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    form_context = {
        "username": "",
        "email": "",
        "salary": "",
        "password": "",
        "password2": "",
    }

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        salary_raw = request.POST.get("salary", "").strip()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")
        form_context.update({
            "username": username,
            "email": email,
            "salary": salary_raw,
        })

        if not username:
            messages.error(request, "Username is required.")
            return render(request, "admin/reception_form.html", form_context)

        if not salary_raw:
            messages.error(request, "Monthly salary is required.")
            return render(request, "admin/reception_form.html", form_context)

        try:
            salary_value = Decimal(salary_raw)
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, "Please enter a valid salary amount.")
            return render(request, "admin/reception_form.html", form_context)

        if salary_value < 0:
            messages.error(request, "Salary cannot be negative.")
            return render(request, "admin/reception_form.html", form_context)

        if password != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "admin/reception_form.html", form_context)

        if len(password) < 4:
            messages.error(request, "Password must be at least 4 characters long.")
            return render(request, "admin/reception_form.html", form_context)

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Username already exists.")
            return render(request, "admin/reception_form.html", form_context)

        user = User.objects.create_user(username=username, password=password, email=email)
        user.is_active = True
        user.is_staff = True
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.is_reception = True
        profile.is_cashier = False
        profile.save()

        messages.success(request, f"Reception user '{username}' created successfully.")
        return redirect("manage_users")

    return render(request, "admin/reception_form.html", form_context)


@login_required
def create_reception(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")

        if not username:
            messages.error(request, "Username is required.")
            return redirect("manage_users")

        if password != password2:
            messages.error(request, "Passwords do not match.")
            return redirect("manage_users")

        if len(password) < 4:
            messages.error(request, "Password must be at least 4 characters long.")
            return redirect("manage_users")

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Username already exists.")
            return redirect("manage_users")

        user = User.objects.create_user(username=username, password=password, email=email)
        user.is_active = True
        user.is_staff = True
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.is_reception = True
        profile.is_cashier = False
        profile.save()

        messages.success(request, f"Reception user '{username}' created successfully.")
        return redirect("manage_users")

    return redirect("manage_users")


@login_required
def delete_user(request, user_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")
    
    from django.contrib.auth.models import User
    
    # Prevent admin from deleting themselves or protected admin accounts
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found.")
        return redirect("manage_users")

    profile, _ = Profile.objects.get_or_create(user=user)
    is_protected_admin = user.is_superuser or (user.is_staff and not profile.is_cashier and not profile.is_reception)

    if user.id == request.user.id:
        messages.error(request, "You cannot delete your own account.")
        return redirect("manage_users")

    if is_protected_admin:
        messages.error(request, f"The protected administrator account '{user.username}' cannot be deleted.")
        return redirect("manage_users")
    
    username = user.username
    user.delete()
    messages.success(request, f"User '{username}' has been deleted successfully.")

    return redirect("manage_users")


@login_required
def toggle_reception_role(request, user_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found.")
        return redirect("manage_users")

    profile, _ = Profile.objects.get_or_create(user=user)
    profile.is_reception = not profile.is_reception
    if profile.is_reception:
        profile.is_cashier = False
    profile.save()

    if profile.is_reception:
        user.is_staff = True
        user.save()
        messages.success(request, f"{user.username} is now a Reception user.")
    else:
        if not user.is_superuser:
            user.is_staff = False
            user.save()
        messages.success(request, f"{user.username} is no longer a Reception user.")

    return redirect("manage_users")


@login_required
def toggle_user_active(request, user_id):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")
    
    from django.contrib.auth.models import User
    
    try:
        user = User.objects.get(id=user_id)
        profile, _ = Profile.objects.get_or_create(user=user)
        is_protected_admin = user.is_superuser or (user.is_staff and not profile.is_cashier and not profile.is_reception)

        if user.id == request.user.id or is_protected_admin:
            messages.error(request, f"The protected administrator account '{user.username}' cannot be deactivated.")
            return redirect("manage_users")

        user.is_active = not user.is_active
        user.save()
        status = "activated" if user.is_active else "deactivated"
        messages.success(request, f"User '{user.username}' has been {status}.")
    except User.DoesNotExist:
        messages.error(request, "User not found.")
    
    return redirect("manage_users")


# =========================
# INVENTORY MANAGEMENT
# =========================
@login_required
def reception_inventory(request):
    """Display inventory dashboard with suit statistics and management options"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    selected_category = request.GET.get('category', 'all').strip().lower() or 'all'
    page_number = request.GET.get('page', 1)
    suits = Suit.objects.all().order_by('-rental_count', 'suit_name')
    
    query = request.GET.get('q', '').strip()
    if query:
        suits = suits.filter(
            Q(suit_name__icontains=query) |
            Q(size__icontains=query) |
            Q(color__icontains=query) |
            Q(collection__icontains=query) |
            Q(description__icontains=query)
        )

    if selected_category != 'all':
        if selected_category == 'accessories':
            suits = suits.filter(
                Q(item_type__in=['ties', 'shoes', 'belts', 'accessories']) |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='tie') | Q(suit_name__icontains='belt') | Q(suit_name__icontains='shoe') |
                Q(description__icontains='tie') | Q(description__icontains='belt') | Q(description__icontains='shoe') |
                Q(description__icontains='accessory')
            )
        elif selected_category == 'shirts':
            suits = suits.filter(
                Q(item_type__iexact='shirts') |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='shirt') | Q(description__icontains='shirt')
            )
        elif selected_category == 'suits':
            suits = suits.filter(
                Q(item_type__iexact='suits') |
                Q(item_type__iexact='') | Q(item_type__isnull=True) |
                Q(suit_name__icontains='suit') | Q(description__icontains='suit')
            )

    status_filter = request.GET.get('status', 'all').strip().lower() or 'all'
    if status_filter != 'all':
        if status_filter == 'retired':
            suits = suits.filter(condition_grade='retired')
        elif status_filter == 'available':
            suits = _available_inventory_queryset(suits)
        elif status_filter == 'out':
            suits = suits.filter(quantity__lte=0)
        else:
            suits = suits.filter(status__iexact=status_filter)

    # Top Rented Suits (Kuw ugu kirada badan)
    top_rented_suits = Suit.objects.order_by('-rental_count')[:5]
    
    # Suits by Condition Grade
    new_suits = suits.filter(condition_grade='new')
    medium_suits = suits.filter(condition_grade='medium')
    retired_suits = suits.filter(condition_grade='retired')
    
    # Stats-ka guud
    total_suits = Suit.objects.count()
    available_count = _available_inventory_queryset().count()
    rented_count = Suit.objects.filter(status='Rented').count()
    cleaning_count = Suit.objects.filter(status='Cleaning').count()
    out_of_stock_count = Suit.objects.filter(quantity=0).count()

    total_revenue = Payment.objects.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    total_expenses = Expense.objects.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_profit = total_revenue - total_expenses
    total_transactions = Payment.objects.count()
    retired_count = Suit.objects.filter(condition_grade='retired').count()
    total_quantity = Suit.objects.aggregate(total=Sum('quantity'))['total'] or 0
    
    total_earnings = sum(suit.total_earnings for suit in suits)
    total_value = sum(suit.price_per_day * suit.quantity for suit in suits)
    
    # Handle rented_quantity for template compatibility
    for suit in suits:
        suit.rented_quantity = suit.suitrequest_set.filter(status='Active').count()

    paginator = Paginator(suits, 10)
    page_obj = paginator.get_page(page_number)
    suits = page_obj.object_list
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1)
    
    # Category counts for reception inventory UI
    suits_count = Suit.objects.filter(
        Q(item_type__iexact='suits') |
        (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='suit') | Q(description__icontains='suit'))
    ).count()
    shirts_count = Suit.objects.filter(
        Q(item_type__iexact='shirts') |
        (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (Q(suit_name__icontains='shirt') | Q(description__icontains='shirt'))
    ).count()
    accessories_count = Suit.objects.filter(
        Q(item_type__in=['ties', 'shoes', 'belts', 'accessories']) |
        (Q(item_type__iexact='') | Q(item_type__isnull=True)) & (
            Q(suit_name__icontains='tie') | Q(suit_name__icontains='belt') | Q(suit_name__icontains='shoe') |
            Q(description__icontains='tie') | Q(description__icontains='belt') | Q(description__icontains='shoe') |
            Q(description__icontains='accessory')
        )
    ).count()

    context = {
        'suits': suits,
        'is_admin_user': is_admin(request.user),
        'page_obj': page_obj,
        'selected_category': selected_category,
        'query': query,
        'status_filter': status_filter,
        'top_rented_suits': top_rented_suits,
        'new_suits': new_suits,
        'medium_suits': medium_suits,
        'retired_suits': retired_suits,
        'total_suits': total_suits,
        'available_count': available_count,
        'rented_count': rented_count,
        'cleaning_count': cleaning_count,
        'out_of_stock_count': out_of_stock_count,
        'retired_count': retired_count,
        'total_quantity': total_quantity,
        'total_revenue': total_revenue,
        'total_expenses': total_expenses,
        'total_profit': total_profit,
        'total_transactions': total_transactions,
        'total_earnings': total_earnings,
        'total_value': total_value,
        'category_counts': {
            'all': total_suits,
            'suits': suits_count,
            'shirts': shirts_count,
            'accessories': accessories_count,
        },
    }
    return render(request, 'reception/reception_inventory.html', context)


@login_required
def add_suit_view(request):
    """Add a new suit to inventory"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    if request.method == 'POST':
        image_file = request.FILES.get('suit_image') or request.FILES.get('image')
        suit_name = request.POST.get('suit_name', '').strip()
        collection = request.POST.get('collection', 'Standard')
        # category is the UI item type (suits, shirts, ties, ...)
        category = request.POST.get('category', 'suits')
        description = request.POST.get('description', '').strip()
        size = request.POST.get('size', 'M')
        color = request.POST.get('color', '').strip()
        try:
            quantity = int(request.POST.get('quantity', 1))
        except (TypeError, ValueError):
            messages.error(request, "Enter a valid whole-number stock quantity.")
            return redirect('add_suit_new')

        if quantity < 0:
            messages.error(request, "Stock quantity cannot be negative.")
            return redirect('add_suit_new')

        price_raw = request.POST.get('price_per_day')
        # Only require price for items that use price (suits/shirts). For others, default to 0.00.
        if price_raw in (None, '') and category not in ['suits', 'shirts']:
            price_per_day = Decimal('0.00')
        else:
            try:
                price_per_day = Decimal(price_raw)
            except (InvalidOperation, TypeError):
                messages.error(request, "Please enter a valid numeric price.")
                return redirect('add_suit_new')

        condition_grade = request.POST.get('condition_grade', 'new')
        damage_notes = request.POST.get('damage_notes', '').strip()

        if not suit_name or not color:
            messages.error(request, "Please fill in all required fields.")
            return redirect('add_suit_new')

        try:
            with transaction.atomic():
                suit = Suit.objects.create(
                    suit_name=suit_name,
                    collection=collection,
                    description=description,
                    size=size,
                    item_type=category,
                    color=color,
                    quantity=quantity,
                    price_per_day=price_per_day,
                    image=image_file,
                    status='Available',
                    condition_grade=condition_grade,
                    damage_notes=damage_notes,
                )
                record_inventory_movement(
                    suit,
                    "opening_balance",
                    quantity_delta=suit.quantity,
                    performed_by=request.user,
                    notes="Opening stock entered when the item was added.",
                )
            messages.success(request, f"Suit '{suit_name}' has been added successfully!")
        except ValidationError as e:
            messages.error(request, f"Error adding suit: {str(e)}")
            return redirect('inventory')

        # After creating, redirect to the suit profile so the admin/reception can review details
        try:
            return redirect('suit_profile', suit.id)
        except Exception:
            return redirect('inventory')
    
    return render(request, 'reception/reception_add_item.html')


@login_required
def edit_suit_view(request, suit_id):
    """Edit a suit's details"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    
    if request.method == 'POST':
        if not _set_inventory_quantity(request, suit, request.POST.get('quantity', suit.quantity)):
            return redirect('edit_suit_new', suit_id=suit_id)
        suit.refresh_from_db()
        suit.suit_name = request.POST.get('suit_name', suit.suit_name)
        suit.collection = request.POST.get('collection', suit.collection)
        suit.description = request.POST.get('description', suit.description)
        suit.size = request.POST.get('size', suit.size)
        suit.item_type = request.POST.get('category', suit.item_type or 'suits')
        suit.color = request.POST.get('color', suit.color)
        suit.price_per_day = request.POST.get('price_per_day', suit.price_per_day)
        suit.condition_grade = request.POST.get('condition_grade', suit.condition_grade)
        suit.damage_notes = request.POST.get('damage_notes', suit.damage_notes)
        
        image_file = request.FILES.get('suit_image') or request.FILES.get('image')
        if image_file:
            suit.image = image_file
        
        suit.save()
        messages.success(request, "Suit has been updated successfully!")
        if is_admin(request.user):
            return redirect('inventory')
        return redirect('inventory')
    
    return render(request, 'admin/edit_suit.html', {'suit': suit})


@login_required
def delete_suit_view(request, suit_id):
    """Delete a suit from inventory"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    suit_name = suit.suit_name
    
    if request.method == 'POST':
        suit.delete()
        messages.success(request, f"Suit '{suit_name}' has been deleted successfully!")
        return redirect('inventory')
    
    return render(request, 'reception/confirm_delete_suit.html', {'suit': suit})


@login_required
def update_suit_status(request, suit_id):
    """Update a suit's rental status"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in ['Available', 'Rented', 'Cleaning']:
            suit.status = new_status
            
            # Haddii la kireeyay, ku dar lacagta
            if new_status == 'Rented':
                rental_days = int(request.POST.get('days', 1))
                earnings = suit.price_per_day * rental_days
                suit.add_earnings(earnings)
                messages.success(request, f"Suit marked as rented! Earnings: ${earnings}")
            else:
                suit.save()
                messages.success(request, f"Status updated to: {suit.get_status_display()}")
        
        return redirect('inventory')
    
    return render(request, 'reception/update_suit_status.html', {'suit': suit})


@login_required
def suit_profile(request, suit_id):
    """Display detailed suit profile with rental history and statistics"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    suit.refresh_rental_stats()
    rental_history = suit.suitrequest_set.all().order_by('-request_date')[:10]
    
    if is_admin(request.user):
        back_url = reverse('inventory')
        profile_template = 'admin/admin_suit_details.html'
    else:
        back_url = reverse('inventory')
        profile_template = 'reception/suit_profile.html'

    context = {
        'suit': suit,
        'rental_history': rental_history,
        'total_rentals': suit.rental_count,
        'total_earnings': suit.total_earnings,
        'back_url': back_url,
    }
    return render(request, profile_template, context)

@login_required
def suit_api_detail(request, suit_id):
    """Return suit data as JSON for AJAX editing in the inventory modal."""
    if not is_reception_or_admin(request.user):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    suit = get_object_or_404(Suit, id=suit_id)
    suit.refresh_rental_stats()
    data = {
        'id': suit.id,
        'suit_name': suit.suit_name,
        'collection': suit.collection,
        'size': suit.size,
        'color': suit.color,
        'quantity': suit.quantity,
        'price_per_day': str(suit.price_per_day),
        'description': suit.description or '',
        'status': suit.status,
        'condition_grade': suit.condition_grade,
        'damage_notes': suit.damage_notes or '',
        'total_earnings': str(suit.total_earnings),
        'rental_count': suit.rental_count,
        'image_url': suit.image.url if getattr(suit, 'image', None) else '',
        'rented_at': suit.rented_at.isoformat() if suit.rented_at else None,
    }
    return JsonResponse(data)


@login_required
def mark_suit_as_dry_cleaning(request, suit_id):
    """Create a reception laundry request or let admin send directly."""
    if not is_reception_or_admin(request.user):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Access denied.'}, status=403)
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    
    if suit.status != "Returned":
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Only returned suits can be marked for dry cleaning.'}, status=400)
        messages.error(request, "Only returned suits can be marked for dry cleaning.")
        return redirect("inventory")
    
    if request.method != 'POST':
        return redirect("inventory")

    active_statuses = ['pending', 'approved', 'sent_to_laundry', 'in_laundry', 'completed']
    if DryCleaning.objects.filter(suit=suit, status__in=active_statuses).exists():
        message = "A laundry request for this item is already active."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': message}, status=409)
        messages.warning(request, message)
        return redirect("inventory")

    rental_request = SuitRequest.objects.filter(suit=suit, status='Returned').order_by('-return_date', '-request_date').first()
    now = timezone.now()
    admin_sent = is_admin(request.user)
    dry_clean = DryCleaning.objects.create(
        suit=suit,
        rental_request=rental_request,
        requested_by=request.user,
        approved_by=request.user if admin_sent else None,
        amount=Decimal('0.00'),
        status='sent_to_laundry' if admin_sent else 'pending',
        notes='Sent directly by admin' if admin_sent else 'Requested by reception; awaiting admin approval',
        approved_at=now if admin_sent else None,
        sent_to_laundry_at=now if admin_sent else None,
    )

    if admin_sent:
        suit.status = "Dry Cleaning"
        suit.save(update_fields=['status'])
        message = f"{suit.suit_name} sent directly to laundry."
        new_status = 'Dry Cleaning'
    else:
        message = f"Laundry request for {suit.suit_name} sent to admin for approval."
        new_status = 'Returned'

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'new_status': new_status, 'request_status': dry_clean.status})
    messages.success(request, message)
    return redirect("inventory")


@login_required
def mark_suit_as_available(request, suit_id):
    """Mark a suit as available after cleaning (Dry Cleaning -> Available)"""
    if not is_reception_or_admin(request.user):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Access denied.'}, status=403)
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    
    if suit.status != "Dry Cleaning":
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Only suits in dry cleaning can be marked as available.'}, status=400)
        messages.error(request, "Only suits in dry cleaning can be marked as available.")
        return redirect("inventory")

    dry_clean = DryCleaning.objects.filter(suit=suit).order_by('-created_at').first()
    if not dry_clean or dry_clean.status not in ['completed', 'approved']:
        message = "The laundry job must be completed before this item can return to inventory."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': message}, status=409)
        messages.error(request, message)
        return redirect("inventory")
    
    if request.method == 'POST':
        with transaction.atomic():
            suit = Suit.objects.select_for_update().get(pk=suit_id)
            dry_clean = DryCleaning.objects.select_for_update().get(pk=dry_clean.pk)
            if suit.status != "Dry Cleaning" or dry_clean.status not in ["completed", "approved"]:
                message = "This cleaning job has already been returned or is not complete."
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'error': message}, status=409)
                messages.error(request, message)
                return redirect("inventory")

            rental_request = dry_clean.rental_request or SuitRequest.objects.filter(
                suit=suit,
                status='Returned',
            ).order_by('-return_date', '-request_date').first()
            if rental_request:
                suit.quantity += rental_request.quantity_requested
            suit.status = "Available"
            suit.save(update_fields=['quantity', 'status'])
            record_inventory_movement(
                suit,
                "cleaning_completed",
                quantity_delta=rental_request.quantity_requested if rental_request else 0,
                performed_by=request.user,
                rental_request=rental_request,
                notes="Cleaning completed; items confirmed returned to stock.",
            )
            dry_clean.status = 'returned'
            dry_clean.returned_at = timezone.now()
            dry_clean.save(update_fields=['status', 'returned_at'])
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'new_status': suit.status})
        messages.success(request, f"{suit.suit_name} is now available for rent.")
        return redirect("inventory")
    
    # For GET requests, just redirect
    return redirect("inventory")
