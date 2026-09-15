from django.http import HttpResponse, JsonResponse
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
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Count
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.utils.dateparse import parse_date
from django.core.mail import send_mail
from django.template.loader import render_to_string
from datetime import datetime, time, timedelta, date
from decimal import Decimal, InvalidOperation

from . import views_public
from .forms import validate_image_upload
from .models import Suit, SuitRequest, Payment, Expense, Notification, FavoriteSuit, Profile, ContactMessage, Staff, Payroll, DryCleaning
from .sms import send_sms


def get_user_profile(user):
    if not user or not user.is_authenticated:
        return None
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def is_cashier(user):
    if not user.is_authenticated:
        return False
    profile = get_user_profile(user)
    return bool(
        (user.is_staff and profile and profile.is_cashier) or
        user.groups.filter(name="Cashier").exists()
    )


def is_reception(user):
    if not user.is_authenticated:
        return False
    profile = get_user_profile(user)
    return bool(
        (user.is_staff and profile and profile.is_reception) or
        user.groups.filter(name="Reception").exists()
    )


def is_admin(user):
    if not user.is_authenticated:
        return False
    profile = get_user_profile(user)
    return bool(
        (user.is_staff and profile and not profile.is_cashier and not profile.is_reception) or
        user.groups.filter(name="Admin").exists()
    )


def is_reception_or_cashier_or_admin(user):
    return is_reception(user) or is_cashier(user) or is_admin(user)


def is_cashier_or_admin(user):
    return is_cashier(user) or is_admin(user)


def is_reception_or_admin(user):
    """Return True if user is reception staff or admin."""
    return is_reception(user) or is_admin(user)


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


@login_required
def payments(request):
    """Dispatch to the appropriate payments view based on user role.

    - Admin users see the admin payments view.
    - Reception/cashier users see the reception payments view.
    """
    if is_admin(request.user):
        return admin_payments(request)

    if is_reception_or_cashier_or_admin(request.user):
        return reception_payments(request)

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
        
        subject = f"Suit Return Reminder - {suit_request.suit.suit_name}"
        
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
            'suit_name': suit_request.suit.suit_name,
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
    if not request.user.is_authenticated or not request.user.is_staff:
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    if is_reception(request.user) or is_cashier(request.user):
        return redirect("reception_dashboard")

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
            suits_qs = suits_qs.filter(status="Available")
            requests_qs = requests_qs.filter(suit__status="Available")
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
        "available_suits_count": suits_qs.filter(status="Available").count(),
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
    return redirect("reception_dashboard")


def reception_dashboard(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
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
        "available_suits_count": suits_qs.filter(status="Available").count(),
        "cleaning_count": cleaning_count,
        "today_revenue": today_revenue,
    }

    return render(request, "reception/reception_dashboard.html", context)


@login_required
def reception_home(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    pending_count = SuitRequest.objects.filter(status="Pending").count()
    active_rentals = SuitRequest.objects.filter(status="Active").order_by("-request_date")
    overdue_requests = [r for r in active_rentals if r.is_overdue]
    total_suits = Suit.objects.count()
    available_count = Suit.objects.filter(status="Available").aggregate(total=Sum('quantity'))['total'] or 0

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
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

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
    available_suits = Suit.objects.filter(quantity__gt=0, status="Available").order_by("suit_name")
    returned_requests = SuitRequest.objects.filter(status="Returned").select_related("suit").order_by("-return_date")
    today_new_rentals = SuitRequest.objects.filter(request_date__date=today).count()
    today_returns = SuitRequest.objects.filter(status="Returned", return_date__date=today).count()
    today_revenue = Payment.objects.filter(payment_date__date=today).aggregate(total=Sum('amount_paid'))['total'] or 0
    pending_balances = SuitRequest.objects.filter(status="Active", is_paid=False).aggregate(total=Sum('total_amount'))['total'] or 0
    net_today = today_revenue
    inventory_total = Suit.objects.count()
    available_stock_units = Suit.objects.filter(status="Available").aggregate(total=Sum('quantity'))['total'] or 0
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
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    prefill = request.session.pop("prefill_customer", None) or {}
    context = _build_booking_desk_context()
    context.update({
        "prefill_first_name": prefill.get("first_name", ""),
        "prefill_father_name": prefill.get("father_name", ""),
        "prefill_grandfather_name": prefill.get("grandfather_name", ""),
        "prefill_phone": prefill.get("phone", ""),
        "prefill_present_address": prefill.get("present_address", ""),
        "prefill_customer_id": prefill.get("customer_id", ""),
    })
    return render(request, "reception/reception.html", context)


@login_required
def admin_booking(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    prefill = request.session.pop("prefill_customer", None) or {}
    context = _build_booking_desk_context()
    context.update({
        "prefill_first_name": prefill.get("first_name", ""),
        "prefill_father_name": prefill.get("father_name", ""),
        "prefill_grandfather_name": prefill.get("grandfather_name", ""),
        "prefill_phone": prefill.get("phone", ""),
        "prefill_present_address": prefill.get("present_address", ""),
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
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        return redirect("reception_reception")

    suit_id_raw = request.POST.get("suit_id", "").strip()
    if not suit_id_raw:
        messages.error(request, "Please select a suit before creating a rental.")
        return redirect("reception_reception")

    try:
        suit_id = int(suit_id_raw)
    except (TypeError, ValueError):
        messages.error(request, "Please select a valid suit before creating a rental.")
        return redirect("reception_reception")

    suit = get_object_or_404(Suit, id=suit_id)
    try:
        quantity_requested = int(request.POST.get("quantity", 1))
        days_requested = int(request.POST.get("days", 1))
    except (TypeError, ValueError):
        messages.error(request, "Please enter valid numeric values for quantity and rental days.")
        return redirect("reception_reception")
    start_date = parse_date(request.POST.get("start_date"))
    end_date = parse_date(request.POST.get("end_date"))
    first_name = request.POST.get("first_name", "").strip()
    father_name = request.POST.get("father_name", "").strip()
    grandfather_name = request.POST.get("grandfather_name", "").strip()
    phone = request.POST.get("phone", "").strip()
    national_id = request.POST.get("id_number") if request.POST.get("has_national_id") else None
    present_address = request.POST.get("present_address", "").strip()
    notes = request.POST.get("notes", "").strip()
    customer_user_id = request.POST.get("customer_user_id", "").strip()
    amount_paid_raw = request.POST.get("amount_paid", "0").strip() or "0"
    payment_method = request.POST.get("payment_method", "cash").strip()
    payment_reference = request.POST.get("payment_reference", "").strip()
    accessories = request.POST.getlist("addons[]")

    try:
        phone = validate_phone_number(phone)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("reception_reception")

    try:
        national_id = validate_national_id(national_id)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("reception_reception")

    image_file = request.FILES.get("customer_image")
    if image_file:
        try:
            if not is_admin(request.user):
                validate_image_upload(image_file, "customer image")
        except forms.ValidationError as e:
            messages.error(request, str(e))
            return redirect("reception_reception")

    try:
        amount_paid = Decimal(amount_paid_raw)
    except (InvalidOperation, TypeError, ValueError):
        messages.error(request, "Please enter a valid amount paid.")
        return redirect("reception_reception")

    if amount_paid < 0:
        messages.error(request, "Amount paid cannot be negative.")
        return redirect("reception_reception")

    if quantity_requested < 1 or quantity_requested > 5:
        messages.error(request, "Please select a quantity between 1 and 5 suits.")
        return redirect("reception_reception")

    if days_requested < 1 or days_requested > 30:
        messages.error(request, "Please select a rental duration between 1 and 30 days.")
        return redirect("reception_reception")

    if quantity_requested > suit.quantity:
        messages.error(request, f"Only {suit.quantity} suits are available for this selection.")
        return redirect("reception_reception")

    if not suit.is_available_for_booking:
        messages.error(request, "The selected suit is not currently available.")
        return redirect("reception_reception")

    if not start_date or not end_date:
        messages.error(request, "Please provide a valid rental start and end date.")
        return redirect("reception_reception")

    if end_date <= start_date:
        messages.error(request, "Return date must be after rental start date.")
        return redirect("reception_reception")

    rental_start_time = timezone.make_aware(datetime.combine(start_date, time.min))
    rental_end_time = timezone.make_aware(datetime.combine(end_date, time.min))

    accessory_total = Decimal("0.00")
    accessory_details = []
    for accessory in accessories:
        price_raw = request.POST.get(f"addon_price_{accessory}", "").strip()
        if not price_raw:
            continue
        try:
            accessory_price = Decimal(price_raw)
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, f"Please enter a valid price for {accessory}.")
            return redirect("reception_reception")
        if accessory_price < 0:
            messages.error(request, "Accessory prices cannot be negative.")
            return redirect("reception_reception")
        accessory_total += accessory_price
        accessory_details.append(f"{accessory.title()}: ${accessory_price:.2f}")

    total_amount = (suit.price_per_day * quantity_requested * days_requested) + (accessory_total * days_requested)
    customer_full_name = " ".join(filter(None, [first_name, father_name, grandfather_name])) or "Guest Customer"

    if amount_paid > total_amount:
        messages.error(request, "Amount paid cannot exceed the total rental amount.")
        return redirect("reception_reception")

    user_for_rental = None
    if customer_user_id:
        try:
            user_for_rental = User.objects.get(id=int(customer_user_id))
        except (TypeError, ValueError, User.DoesNotExist):
            user_for_rental = None

    rental = SuitRequest.objects.create(
        suit=suit,
        user=user_for_rental,
        first_name=first_name or None,
        second_name=father_name or None,
        name=customer_full_name,
        phone=phone,
        customer_email=request.user.email if request.user.is_authenticated else None,
        national_id=national_id,
        present_address=present_address or None,
        parents_address=None,
        notes=notes or None,
        rental_start_time=rental_start_time,
        end_time=rental_end_time,
        days_requested=days_requested,
        quantity_requested=quantity_requested,
        total_amount=total_amount,
        status="Active",
        is_paid=False,
        payment_method=payment_method.title() if amount_paid > 0 else "",
        payment_reference=payment_reference if amount_paid > 0 else None,
    )

    suit.quantity -= quantity_requested
    suit.status = "Rented"
    suit.save()
    suit.refresh_rental_end_time()

    if amount_paid > 0:
        payment_status = 'paid' if amount_paid >= total_amount else 'partial'
        payment = Payment.objects.create(
            rental=rental,
            cashier=request.user,
            amount_paid=amount_paid,
            payment_method=payment_method.title() if payment_method else 'Cash',
            payment_reference=payment_reference or None,
            status=payment_status,
            payment_date=timezone.now(),
        )
        payment.receipt_number = f"RCPT-{payment.id:06d}"
        payment.save(update_fields=['receipt_number'])

        if amount_paid >= total_amount:
            rental.is_paid = True
            rental.save(update_fields=['is_paid'])

    messages.success(request, "New rental was created successfully.")
    return redirect("reception_reception")


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

    return render(request, "admin/receipt.html", {
        "req": req,
        "company_name": "Suit Rental",
        "company_address": "123 Main Street, City",
        "company_phone": "+252 61 234 5678",
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
        "line_items[0][price_data][product_data][name]": f"{req.quantity_requested} x {req.suit.suit_name} rental",
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

    requests = SuitRequest.objects.all().order_by("-request_date")

    status_filter = request.GET.get("status")
    if status_filter:
        if status_filter == "pending":
            requests = requests.filter(status="Pending")
        elif status_filter == "reserved":
            requests = requests.filter(status__in=["Approved", "Reserved"])
        elif status_filter == "active":
            requests = requests.filter(status="Active")
        elif status_filter == "returned":
            requests = requests.filter(status="Returned")
        elif status_filter == "available":
            requests = requests.filter(suit__status="Available")
        elif status_filter == "dry-cleaning":
            requests = requests.filter(suit__status="Dry Cleaning")
        else:
            requests = requests.filter(status=request.GET.get("status"))

    if request.GET.get("q"):
        q = request.GET.get("q")
        requests = requests.filter(
            Q(name__icontains=q) |
            Q(phone__icontains=q) |
            Q(suit__suit_name__icontains=q)
        )

    available_count = Suit.objects.filter(status="Available").count()
    reserved_count = Suit.objects.filter(status="Reserved").count()
    rented_count = Suit.objects.filter(status="Rented").count()
    returned_count = Suit.objects.filter(status="Returned").count()
    dry_cleaning_count = Suit.objects.filter(status="Dry Cleaning").count()

    context = {
        "requests": requests,
        "active_requests": SuitRequest.objects.filter(status="Active").order_by("-request_date"),
        "total_count": SuitRequest.objects.count(),
        "pending_count": SuitRequest.objects.filter(status="Pending").count(),
        "approved_count": SuitRequest.objects.filter(status="Approved").count(),
        "rejected_count": SuitRequest.objects.filter(status="Rejected").count(),
        "active_count": SuitRequest.objects.filter(status="Active").count(),
        "returned_count": returned_count,
        "expired_count": SuitRequest.objects.filter(status="Expired").count(),
        "current_q": request.GET.get("q", ""),
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
                return redirect("reception_requests")

            suit_request = SuitRequest.objects.filter(id=request_id, status="Returned").select_related("suit").first()
            if not suit_request:
                messages.error(request, "That rental request is not available for this action.")
                return redirect("reception_requests")

            dry_clean = DryCleaning.objects.filter(suit_id=suit_request.suit.id, status="approved").order_by("-created_at").first()
            if not dry_clean:
                messages.error(request, f"Admin approval is still pending for '{suit_request.suit.suit_name}'.")
                return redirect("reception_requests")

            with transaction.atomic():
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

                suit = suit_request.suit
                suit.quantity += suit_request.quantity_requested
                suit.status = "Available"
                suit.save(update_fields=["quantity", "status"])

                suit_request.status = "Returned"
                suit_request.save(update_fields=["status"])

                dry_clean.paid = True
                dry_clean.status = "approved"
                dry_clean.save(update_fields=["paid", "status"])

                messages.success(request, f"{suit.suit_name} is now ready and removed from dry cleaning.")

            return redirect("reception_requests")

    status_filter = request.GET.get("status", "all")
    page = request.GET.get("page", 1)
    requests = SuitRequest.objects.all().select_related('suit').prefetch_related('suit__dry_cleanings').order_by("-request_date")

    if status_filter == "pending":
        requests = requests.filter(status="Pending")
    elif status_filter == "reserved":
        requests = requests.filter(status__in=["Approved", "Reserved"])
    elif status_filter == "active":
        requests = requests.filter(status="Active")
    elif status_filter == "returned":
        requests = requests.filter(status="Returned")
    elif status_filter == "dry-cleaning":
        requests = requests.filter(suit__status="Dry Cleaning")
    elif status_filter == "available":
        requests = requests.filter(suit__status="Available")

    paginator = Paginator(requests, 10)
    page_obj = paginator.get_page(page)

    recent_rentals = SuitRequest.objects.filter(status="Returned").select_related('suit').order_by("-return_date")[:10]

    available_count = Suit.objects.filter(status="Available").count()
    reserved_count = Suit.objects.filter(status="Reserved").count()
    rented_count = Suit.objects.filter(status="Rented").count()
    returned_count = Suit.objects.filter(status="Returned").count()
    dry_cleaning_count = Suit.objects.filter(status="Dry Cleaning").count()

    context = {
        'requests': page_obj.object_list,
        'page_obj': page_obj,
        'recent_rentals': recent_rentals,
        'active_requests': SuitRequest.objects.filter(status="Active").order_by("-request_date"),
        'total_count': SuitRequest.objects.count(),
        'pending_count': SuitRequest.objects.filter(status="Pending").count(),
        'approved_count': SuitRequest.objects.filter(status="Approved").count(),
        'active_count': SuitRequest.objects.filter(status="Active").count(),
        'rejected_count': SuitRequest.objects.filter(status="Rejected").count(),
        'returned_count': SuitRequest.objects.filter(status="Returned").count(),
        'expired_count': SuitRequest.objects.filter(status="Expired").count(),
        'current_status': status_filter,
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
        customer_requests = SuitRequest.objects.filter(user=request.user).order_by("-request_date")
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
        return redirect("reception_requests")
    
    # Get first request to grab customer details
    first_req = customer_requests.first()
    
    # Calculate stats
    total_rentals = customer_requests.count()
    active_rentals = customer_requests.filter(status="Active").count()
    returned_rentals = customer_requests.filter(status="Returned").count()
    pending_rentals = customer_requests.filter(status="Pending").count()
    total_spent = customer_requests.filter(status__in=["Active", "Returned"]).aggregate(total=Sum("total_amount"))['total'] or 0
    
    context = {
        'customer_name': first_req.name,
        'customer_phone': first_req.phone,
        'customer_id': first_req.national_id,
        'customer_image': first_req.customer_image,
        'requests': customer_requests,
        'total_rentals': total_rentals,
        'active_rentals': active_rentals,
        'returned_rentals': returned_rentals,
        'pending_rentals': pending_rentals,
        'total_spent': total_spent,
    }
    return render(request, "admin/customer_detail.html", context)


@login_required
def customer_detail_by_user(request, user_id):
    if not is_reception_or_cashier_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Cashier/Admin only.")
        return redirect("home")

    customer_user = get_object_or_404(User, id=user_id)
    customer_requests = SuitRequest.objects.filter(user=customer_user).order_by("-request_date")

    if not customer_requests.exists():
        customer_requests = SuitRequest.objects.filter(name=customer_user.get_full_name() or customer_user.username).order_by("-request_date")

    if not customer_requests.exists():
        messages.error(request, "Customer not found.")
        return redirect("manage_users")

    first_req = customer_requests.first()
    total_rentals = customer_requests.count()
    active_rentals = customer_requests.filter(status="Active").count()
    returned_rentals = customer_requests.filter(status="Returned").count()
    pending_rentals = customer_requests.filter(status="Pending").count()
    total_spent = customer_requests.filter(status__in=["Active", "Returned"]).aggregate(total=Sum("total_amount"))["total"] or 0

    context = {
        "customer_name": first_req.name or customer_user.get_full_name() or customer_user.username,
        "customer_phone": first_req.phone or "",
        "customer_id": first_req.national_id or customer_user.username,
        "customer_image": first_req.customer_image,
        "customer_user": customer_user,
        "requests": customer_requests,
        "total_rentals": total_rentals,
        "active_rentals": active_rentals,
        "returned_rentals": returned_rentals,
        "pending_rentals": pending_rentals,
        "total_spent": total_spent,
    }
    return render(request, "admin/customer_detail.html", context)


@login_required
def new_rent_for_customer(request, user_id):
    if not is_reception_or_cashier_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Cashier/Admin only.")
        return redirect("home")

    customer_user = get_object_or_404(User, id=user_id)
    recent_request = SuitRequest.objects.filter(user=customer_user).order_by("-request_date").first()

    request.session["prefill_customer"] = {
        "customer_id": customer_user.id,
        "first_name": customer_user.first_name or getattr(recent_request, "first_name", "") or "",
        "father_name": customer_user.last_name or getattr(recent_request, "second_name", "") or "",
        "grandfather_name": "",
        "phone": getattr(recent_request, "phone", "") or "",
        "present_address": getattr(recent_request, "present_address", "") or "",
    }

    if is_admin(request.user):
        return redirect("admin_booking")
    return redirect("reception_reception")


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
                message=f"Email reminder sent to {req.name} for {req.suit.suit_name}"
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
        return redirect('reception_dashboard')

    req = get_object_or_404(SuitRequest, id=req_id)

    if request.method == 'POST':
        message_text = request.POST.get('message', '').strip()
        if not message_text:
            messages.error(request, 'Message cannot be empty.')
            return redirect('reception_send_sms', req_id=req_id)

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
    default_message = f"Salaan {req.name}, xusuusin: wakhtiga kirada ee '{req.suit.suit_name}' ayaa la joogo. Fadlan soo celi ama la xiriir {request.user.username}."
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
    return_url = "requests" if is_admin(request.user) else "reception_requests"

    pickup_mode_default = False
    action_mode_default = 'rent_now'
    if request.method == "GET":
        pickup_mode_default = request.GET.get('pickup') == '1' or request.GET.get('mode') == 'pickup'
        if req.status == 'Reserved' and not pickup_mode_default:
            action_mode_default = 'reserve'
    
    if req.status != "Pending" and req.status != 'Reserved':
        messages.error(request, "Only pending or reserved requests can be approved.")
        return redirect(return_url)

    if request.method == "POST":
        start_date_raw = request.POST.get("start_date")
        # request.POST may sometimes contain non-string date objects depending on middleware or JS
        if isinstance(start_date_raw, datetime):
            start_date = start_date_raw.date()
        elif isinstance(start_date_raw, date):
            start_date = start_date_raw
        else:
            # Only parse when we have a string; guard against lists/bytes/other types
            if isinstance(start_date_raw, bytes):
                try:
                    start_date_raw = start_date_raw.decode('utf-8')
                except Exception:
                    start_date = None
                    start_date_raw = None

            if isinstance(start_date_raw, str):
                start_date = parse_date(start_date_raw)
            else:
                start_date = None
        amount_paid_raw = request.POST.get("amount_paid", "0").strip() or "0"
        payment_method = request.POST.get("payment_method", "cash").strip()
        payment_reference = request.POST.get("payment_reference", "").strip()
        accessories = request.POST.getlist("accessories")
        action_mode = request.POST.get("action_mode", "rent_now").strip()
        id_document_type = request.POST.get("id_document_type", "National ID").strip()
        id_document_number = request.POST.get("id_document_number", "").strip()
        reserve_duration_raw = request.POST.get("reserve_duration", "24").strip()
        reserve_custom_hours_raw = request.POST.get("reserve_custom_hours", "").strip()

        try:
            amount_paid = Decimal(amount_paid_raw)
        except (InvalidOperation, TypeError, ValueError):
            messages.error(request, "Please enter a valid payment amount.")
            return redirect("approve_request", req.id)

        accessory_prices = {
            "shoes": Decimal("5.00"),
            "tie": Decimal("1.50"),
            "shirt": Decimal("3.00"),
            "belt": Decimal("1.00"),
        }
        accessory_total = sum(accessory_prices.get(item, Decimal("0.00")) for item in accessories)
        total_amount = (req.suit.price_per_day * req.days_requested * req.quantity_requested) + (accessory_total * req.days_requested * req.quantity_requested)

        if amount_paid < 0:
            messages.error(request, "Amount paid cannot be negative.")
            return redirect("approve_request", req.id)

        if amount_paid > total_amount:
            messages.error(request, "Amount paid cannot exceed the total rental amount.")
            return redirect("approve_request", req.id)

        if req.quantity_requested > req.suit.quantity:
            messages.error(request, f"Only {req.suit.quantity} suits are available for this request.")
            return redirect("approve_request", req.id)

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

        notes = req.notes or ""
        req.payment_method = payment_method.title() if payment_method else 'Cash'
        req.payment_reference = payment_reference or None
        if id_document_number:
            if notes:
                notes += "\n"
            notes += f"{id_document_type}: {id_document_number}"
            if id_document_type == "National ID":
                req.national_id = id_document_number

        accessory_details = []
        if "shoes" in accessories:
            accessory_details.append(f"shoes ({request.POST.get('shoes_size', '')} / {request.POST.get('shoes_color', '')})")
        if "tie" in accessories:
            accessory_details.append(f"tie ({request.POST.get('tie_size', '')} / {request.POST.get('tie_color', '')})")
        if "shirt" in accessories:
            accessory_details.append(f"shirt ({request.POST.get('shirt_size', '')} / {request.POST.get('shirt_color', '')})")
        if "belt" in accessories:
            accessory_details.append(f"belt ({request.POST.get('belt_size', '')} / {request.POST.get('belt_color', '')})")

        if accessory_details:
            if notes:
                notes += "\n"
            notes += f"Accessories: {', '.join(accessory_details)}"

        if start_date:
            rental_start_time = timezone.make_aware(datetime.combine(start_date, time.min))
        else:
            rental_start_time = timezone.now() if action_mode == "rent_now" else None

        if action_mode == "reserve":
            req.status = "Reserved"
            req.payment_method = payment_method.title() if payment_method else 'Cash'
            req.payment_reference = payment_reference or None
            if not req.approved_at:
                req.approved_at = timezone.now()
            if rental_start_time:
                req.rental_start_time = rental_start_time
                req.due_date = rental_start_time + timedelta(hours=reserve_hours)
            else:
                req.due_date = timezone.now() + timedelta(hours=reserve_hours)
            if notes:
                notes += "\n"
            notes += f"Reserved pickup for {reserve_hours} hours"
            req.notes = notes.strip()
            req.total_amount = total_amount
            req.save(update_fields=['status', 'approved_at', 'due_date', 'rental_start_time', 'total_amount', 'notes', 'national_id', 'payment_method', 'payment_reference'])

            req.suit.status = "Reserved"
            req.suit.save(update_fields=['status'])

            if amount_paid > 0:
                payment_status = 'paid' if amount_paid >= total_amount else 'partial'
                payment = Payment.objects.create(
                    rental=req,
                    cashier=request.user,
                    amount_paid=amount_paid,
                    payment_method=payment_method.title() if payment_method else 'Cash',
                    payment_reference=payment_reference or None,
                    status=payment_status,
                    payment_date=timezone.now(),
                )
                payment.receipt_number = f"RCPT-{payment.id:06d}"
                payment.save(update_fields=['receipt_number'])

                if amount_paid >= total_amount:
                    req.is_paid = True
                    req.save(update_fields=['is_paid'])

            messages.success(request, "Request reserved for pickup successfully.")
            return redirect(return_url)

        # Set suit status to Reserved before starting rental
        req.suit.status = "Reserved"
        req.suit.save()

        req.rent_days = req.days_requested or 1
        req.approved_at = timezone.now()
        req.due_date = req._default_due_date(req.approved_at)
        req.notes = notes.strip()
        req.total_amount = total_amount
        req.save(update_fields=['rent_days', 'approved_at', 'due_date', 'notes', 'total_amount', 'national_id'])

        req.start_rental(start_time=rental_start_time)

        if amount_paid > 0:
            payment_status = 'paid' if amount_paid >= total_amount else 'partial'
            payment = Payment.objects.create(
                rental=req,
                cashier=request.user,
                amount_paid=amount_paid,
                payment_method=payment_method.title() if payment_method else 'Cash',
                payment_reference=payment_reference or None,
                status=payment_status,
                payment_date=timezone.now(),
            )
            payment.receipt_number = f"RCPT-{payment.id:06d}"
            payment.save(update_fields=['receipt_number'])

            if amount_paid >= total_amount:
                req.is_paid = True
                req.save(update_fields=['is_paid'])

        messages.success(request, "Request approved and rental started successfully.")
        return redirect(return_url)

    total_amount = req.suit.price_per_day * req.days_requested * req.quantity_requested
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
    }
    template_name = "admin/admin_approve.html" if is_admin(request.user) else "reception/reception_approve.html"
    return render(request, template_name, context)


# =========================
# REJECT REQUEST
# =========================
@login_required
def reject_request(request, req_id):
    if not request.user.is_staff:
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    req = get_object_or_404(SuitRequest, id=req_id)

    if req.status == "Pending":
        req.status = "Rejected"
        req.save()

        messages.success(request, "Request rejected")

    if is_cashier(request.user):
        return redirect("reception_requests")
    return redirect("requests")


# =========================
# RETURN SUIT
# =========================
@login_required
def return_suit(request, req_id):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    req = get_object_or_404(SuitRequest, id=req_id)

    if req.status == "Active":
        req.return_suit()
        late_fee = req.late_fee
        if late_fee > 0:
            messages.success(request, f"Suit returned successfully. Late fee applied: ${late_fee}.")
        else:
            messages.success(request, "Suit returned successfully")

    if is_cashier(request.user):
        return redirect("reception_requests")
    return redirect("requests")


@login_required
def process_return(request):
    if request.method != "POST":
        return redirect("reception_reception")

    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    rental_id = request.POST.get("rental_id")
    return_notes = request.POST.get("return_notes", "")
    actual_return_date_str = request.POST.get("actual_return_date")
    return_discount = request.POST.get("return_discount", "0")
    return_amount_paid = request.POST.get("return_amount_paid", "0")

    req = get_object_or_404(SuitRequest, id=rental_id)

    # parse actual return date if provided
    parsed_date = None
    if actual_return_date_str:
        parsed_date = parse_date(actual_return_date_str)

    if parsed_date:
        # combine parsed date with current time and make tz-aware
        return_dt = timezone.make_aware(datetime.combine(parsed_date, datetime.now().time()))
    else:
        return_dt = timezone.now()

    # Calculate late fee based on provided actual return datetime
    late_fee = req._calculate_late_fee(return_dt)

    # Update request fields similarly to return_suit(), but using provided date
    req.return_date = return_dt
    returned_items = (request.POST.get("returned_items") or "").strip()
    if returned_items:
        existing_notes = req.notes or ""
        req.notes = f"{existing_notes}\nReturned items: {returned_items}" if existing_notes else f"Returned items: {returned_items}"
    # Add late fee to total_amount (only if positive)
    try:
        if late_fee and late_fee > 0:
            req.total_amount = (req.total_amount or 0) + late_fee
    except Exception:
        pass

    # Apply discount if any
    try:
        discount_dec = Decimal(return_discount)
        if discount_dec and discount_dec > 0:
            # Reduce total_amount by discount
            req.total_amount = max(Decimal(req.total_amount) - discount_dec, Decimal('0.00'))
    except (InvalidOperation, TypeError, ValueError):
        discount_dec = Decimal('0.00')

    # Mark returned
    req.status = 'Returned'
    # Append return notes to existing notes
    if return_notes:
        existing = req.notes or ''
        req.notes = f"{existing}\nReturn notes: {return_notes}" if existing else f"Return notes: {return_notes}"

    req.save()

    # Move the suit into cleaning only when it should follow the laundry workflow.
    suit = req.suit
    if suit.should_queue_for_dry_cleaning:
        suit.status = 'Dry Cleaning'
    else:
        suit.quantity += req.quantity_requested or 1
        suit.status = 'Available'
    suit.save(update_fields=['status', 'quantity'])
    try:
        suit.refresh_rental_end_time()
    except Exception:
        pass

    # Record payment if any amount collected at return
    try:
        paid_dec = Decimal(return_amount_paid)
    except (InvalidOperation, TypeError, ValueError):
        paid_dec = Decimal('0.00')

    if paid_dec and paid_dec > 0:
        Payment.objects.create(
            rental=req,
            cashier=request.user if request.user.is_authenticated else None,
            amount_paid=paid_dec,
            payment_method=request.POST.get('payment_method', 'cash'),
            notes=f"Return collection. Discount applied: {discount_dec if 'discount_dec' in locals() else 0}",
        )

    # Update paid flag if fully paid
    try:
        if req.balance_due <= 0:
            req.is_paid = True
            req.save(update_fields=['is_paid'])
    except Exception:
        pass

    if suit.should_queue_for_dry_cleaning:
        messages.success(request, f"Suit return processed and queued for cleaning. Remaining balance: ${req.balance_due}.")
    else:
        messages.success(request, f"Suit return processed and item restored to stock. Remaining balance: ${req.balance_due}.")
    return redirect('laundry_backlog')


@login_required
def process_reservation(request):
    if request.method != 'POST':
        return redirect('requests')

    if not is_admin(request.user) and not is_reception(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    return_url = 'requests' if is_admin(request.user) else 'reception_requests'

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

    req.status = "Reserved"
    req.notes = (req.notes or "") + f"\nReserved for {duration_hours} hours."
    req.save(update_fields=['status', 'notes'])

    suit = req.suit
    suit.status = "Reserved"
    suit.save(update_fields=['status'])

    messages.success(request, "Rental request has been reserved successfully.")
    return redirect(return_url)


@login_required
def return_suit_view(request, request_id):
    """Simple endpoint to mark a suit request as returned and enqueue it for cleaning.

    This mirrors the lightweight behavior in the snippet provided by the user:
    - Marks the request `Returned` if it was previously Approved/Active
    - Does NOT modify suit quantity here (cleaning will restore availability)
    - Redirects back to the reception requests page.
    """
    suit_request = get_object_or_404(SuitRequest, id=request_id)

    if suit_request.status in ("Approved", "Active"):
        suit_request.return_date = timezone.now()
        suit_request.status = "Returned"
        suit_request.save()

        suit = suit_request.suit
        if suit:
            if suit.should_queue_for_dry_cleaning:
                suit.status = 'Dry Cleaning'
            else:
                suit.quantity += suit_request.quantity_requested or 1
                suit.status = 'Available'
            suit.save(update_fields=['status', 'quantity'])
            try:
                suit.refresh_rental_end_time()
            except Exception:
                pass

        if suit and suit.should_queue_for_dry_cleaning:
            messages.info(request, f"📦 {suit_request.suit.suit_name} waa la soo celiyey, waxaana la geliyey safka dhar-dhaqista (Waiting for Cleaning).")
        else:
            messages.info(request, f"📦 {suit_request.suit.suit_name} waa la soo celiyey, waxaana dib loogu celiyey keydka.")
    else:
        messages.error(request, "Dalabkan hadda ma aha mid furan oo la soo celin karo.")

    return redirect('reception_requests')


@login_required
def return_suit_audit_view(request, request_id):
    """Show an audit/receive page before confirming a return and sending to laundry.

    Displays total, paid and remaining debt and on POST marks the request `Returned`
    and redirects to the laundry backlog page.
    """
    suit_request = get_object_or_404(SuitRequest, id=request_id)

    # Calculate debt: price_per_day * days_requested * quantity_requested
    total_amount = (suit_request.suit.price_per_day or 0) * (getattr(suit_request, 'days_requested', 0) or 0) * (suit_request.quantity_requested or 0)
    amount_paid = getattr(suit_request, 'amount_paid', 0) or 0
    try:
        remaining_debt = total_amount - amount_paid
    except Exception:
        remaining_debt = total_amount

    if request.method == 'POST':
        suit_request.return_date = timezone.now()
        suit_request.status = 'Returned'
        suit_request.save()

        suit = suit_request.suit
        if suit:
            if suit.should_queue_for_dry_cleaning:
                suit.status = 'Dry Cleaning'
            else:
                suit.quantity += suit_request.quantity_requested or 1
                suit.status = 'Available'
            suit.save(update_fields=['status', 'quantity'])
            try:
                suit.refresh_rental_end_time()
            except Exception:
                pass

        if suit and suit.should_queue_for_dry_cleaning:
            messages.success(request, f"📦 {suit_request.suit.suit_name} waa la gudoomay, waxaana loo wareejiyey dhar-dhaqista.")
        else:
            messages.success(request, f"📦 {suit_request.suit.suit_name} waa la soo celiyey, waxaana dib loogu celiyey keydka.")
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
    if not (is_admin(request.user) or is_reception(request.user)):
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

            suit.suit_name = request.POST.get("suit_name", suit.suit_name)
            suit.collection = collection or suit.collection
            suit.description = description or suit.description
            suit.size = request.POST.get("size", suit.size)
            suit.item_type = category or suit.item_type or "suits"
            suit.color = request.POST.get("color", suit.color)
            suit.price_per_day = price
            suit.quantity = int(request.POST.get("quantity", suit.quantity))
            suit.condition_grade = condition_grade
            suit.damage_notes = damage_notes
            if image_file:
                suit.image = image_file
            suit.save()
            messages.success(request, "Suit updated successfully!")
        else:
            Suit.objects.create(
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
            messages.success(request, "Suit added successfully!")

        if is_reception(request.user) or is_cashier(request.user):
            return redirect("reception_inventory")
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
            suits = suits.filter(quantity__gt=0, status__iexact='Available')
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
    available_count = Suit.objects.filter(quantity__gt=0).count()
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
    if not (is_admin(request.user) or is_reception(request.user)):
        messages.error(request, "Access denied.")
        return redirect("home")

    suit = get_object_or_404(Suit, id=suit_id)

    if request.method == "POST":
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

        suit.suit_name = request.POST["suit_name"]
        suit.collection = collection
        suit.size = request.POST["size"]
        suit.item_type = request.POST.get("category", suit.item_type or "suits")
        suit.color = request.POST["color"]
        suit.price_per_day = price
        suit.quantity = int(request.POST.get("quantity", suit.quantity))
        suit.status = request.POST["status"]

        if request.FILES.get("image"):
            suit.image = request.FILES["image"]
        elif request.POST.get("clear_image"):
            suit.image = None

        suit.save()
        messages.success(request, "Suit updated successfully!")
        if is_reception(request.user) or is_cashier(request.user):
            return redirect("reception_inventory")
        return redirect("dashboard")

    return render(request, "admin/edit_suit.html", {"suit": suit})


# =========================
# DELETE SUIT
# =========================
@login_required
def delete_suit(request, suit_id):
    if not (is_admin(request.user) or is_reception(request.user)):
        messages.error(request, "Access denied.")
        return redirect("home")

    Suit.objects.get(id=suit_id).delete()
    if is_reception(request.user) or is_cashier(request.user):
        return redirect("reception_inventory")
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
            suits = suits.filter(quantity__gt=0, status__iexact='Available')
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
    available_count = Suit.objects.filter(quantity__gt=0).count()
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

    return render(request, "admin/admin_profile.html")


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
@login_required
def reports(request):
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")

    now = timezone.localtime()
    
    # New date range parameters (no restrictions)
    from_date_str = request.GET.get('from_date', '').strip()
    to_date_str = request.GET.get('to_date', '').strip()
    
    # Keep old parameters for backward compatibility
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
    
    # Priority: use new from_date/to_date if provided
    if from_date_str or to_date_str:
        try:
            if from_date_str:
                parsed_from = parse_date(from_date_str)
                if parsed_from:
                    filter_start = timezone.make_aware(datetime.combine(parsed_from, time.min), timezone.get_current_timezone())
            if to_date_str:
                parsed_to = parse_date(to_date_str)
                if parsed_to:
                    filter_end = timezone.make_aware(datetime.combine(parsed_to, time.max), timezone.get_current_timezone())
            # Validate date range
            if filter_start and filter_end and filter_start > filter_end:
                date_range_error = "From date must be earlier than or equal to To date."
        except Exception as e:
            date_range_error = f"Invalid date format: {str(e)}"
    
    # Fallback to old parameters if no new date range provided
    elif selected_date:
        parsed_date = parse_date(selected_date)
        if parsed_date:
            filter_start = timezone.make_aware(datetime.combine(parsed_date, time.min), timezone.get_current_timezone())
            filter_end = timezone.make_aware(datetime.combine(parsed_date, time.max), timezone.get_current_timezone())
    elif selected_month:
        try:
            year_str, month_str = selected_month.split('-')
            year = int(year_str)
            month = int(month_str)
            filter_start, filter_end = get_month_range(year, month)
        except (ValueError, TypeError):
            selected_month = ''
    elif selected_recent_month:
        try:
            recent_months = int(selected_recent_month)
            year = now.year
            month = now.month - recent_months + 1
            while month <= 0:
                month += 12
                year -= 1
            filter_start, _ = get_month_range(year, month)
            filter_end = now
        except (ValueError, TypeError):
            selected_recent_month = ''
    elif month_range == 'current':
        filter_start, filter_end = get_month_range_12_25(now.year, now.month)
    elif month_range == 'last':
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

    # Suit Statistics
    available_quantity = Suit.objects.aggregate(total=Sum('quantity'))['total'] or 0
    rented_quantity = SuitRequest.objects.filter(status="Active").aggregate(total=Sum('quantity_requested'))['total'] or 0
    total_quantity = available_quantity + rented_quantity
    total_suits = Suit.objects.count()
    
    # Request Statistics
    filtered_requests = SuitRequest.objects.select_related('suit').order_by('-request_date')
    filtered_payments = Payment.objects.select_related('rental').order_by('-payment_date')
    filtered_expenses = Expense.objects.order_by('-expense_date')
    staff_list = Staff.objects.order_by('name')

    # Apply date filters (no restriction on future dates if explicitly requested)
    if filter_start and filter_end:
        filtered_requests = filtered_requests.filter(request_date__range=(filter_start, filter_end))
        filtered_payments = filtered_payments.filter(payment_date__range=(filter_start, filter_end))
        filtered_expenses = filtered_expenses.filter(expense_date__range=(filter_start, filter_end))
    elif filter_start:
        filtered_requests = filtered_requests.filter(request_date__gte=filter_start)
        filtered_payments = filtered_payments.filter(payment_date__gte=filter_start)
        filtered_expenses = filtered_expenses.filter(expense_date__gte=filter_start)
    elif filter_end:
        filtered_requests = filtered_requests.filter(request_date__lte=filter_end)
        filtered_payments = filtered_payments.filter(payment_date__lte=filter_end)
        filtered_expenses = filtered_expenses.filter(expense_date__lte=filter_end)

    total_requests = filtered_requests.count()
    pending_requests = filtered_requests.filter(status="Pending").count()
    approved_requests = filtered_requests.filter(status="Approved").count()
    active_requests = filtered_requests.filter(status="Active").count()
    returned_requests = filtered_requests.filter(status="Returned").count()
    rejected_requests = filtered_requests.filter(status="Rejected").count()
    expired_requests = filtered_requests.filter(status="Expired").count()
    
    # Financial Data
    revenue = filtered_requests.filter(status__in=["Active", "Returned"]).aggregate(
        total=Sum('total_amount')
    )['total'] or 0

    # Calculate utilization rate
    utilization_rate = 0
    if total_quantity > 0:
        utilization_rate = round((rented_quantity / total_quantity) * 100, 1)
    
    # Suit type breakdown and inventory mix
    luxury_items = Suit.objects.filter(collection="Luxury")
    standard_items = Suit.objects.filter(collection="Standard")
    budget_items = Suit.objects.filter(collection="Budget")

    luxury_count = luxury_items.count()
    standard_count = standard_items.count()
    budget_count = budget_items.count()
    luxury_units = luxury_items.aggregate(total=Sum('quantity'))['total'] or 0
    standard_units = standard_items.aggregate(total=Sum('quantity'))['total'] or 0
    budget_units = budget_items.aggregate(total=Sum('quantity'))['total'] or 0
    inventory_mix_total = luxury_units + standard_units + budget_units
    luxury_mix_pct = round((luxury_units / inventory_mix_total * 100), 1) if inventory_mix_total else 0
    standard_mix_pct = round((standard_units / inventory_mix_total * 100), 1) if inventory_mix_total else 0
    budget_mix_pct = round((budget_units / inventory_mix_total * 100), 1) if inventory_mix_total else 0
    revenue_potential = sum(
        (item.quantity or 0) * (item.price_per_day or Decimal('0'))
        for item in Suit.objects.all()
    )
    
    total_expenses = filtered_expenses.aggregate(total=Sum('amount'))['total'] or 0
    
    # Calculate dry cleaning expenses separately
    if filter_start and filter_end:
        dry_cleaning_expenses = DryCleaning.objects.filter(
            date__range=(filter_start.date(), filter_end.date()),
            status='approved'  # Only count approved dry cleaning
        ).aggregate(total=Sum('amount'))['total'] or 0
    else:
        dry_cleaning_expenses = DryCleaning.objects.filter(
            status='approved'
        ).aggregate(total=Sum('amount'))['total'] or 0
    
    # Calculate profit scenarios
    net_profit = revenue - total_expenses  # Profit after ALL expenses (including dry cleaning)
    net_profit_before_cleaning = revenue - (total_expenses - dry_cleaning_expenses)  # Profit before dry cleaning cost deduction
    
    payment_count = filtered_payments.count()
    expense_count = filtered_expenses.count()

    payments = filtered_payments[:100]
    expenses = filtered_expenses[:100]
    requests = filtered_requests[:100]

    all_activities = []
    for expense in expenses:
        all_activities.append({
            'id': expense.id,
            'type': 'Expense',
            'date': expense.expense_date,
            'category': expense.notes or 'Expense',
            'description': expense.description,
            'reference': expense.created_by.username if expense.created_by else '-',
            'amount': expense.amount,
        })
    for payment in payments:
        all_activities.append({
            'id': payment.id,
            'type': 'Transaction',
            'date': payment.payment_date,
            'category': payment.payment_method or 'Payment',
            'description': f"Payment for {payment.rental.suit.suit_name if payment.rental and payment.rental.suit else 'Rental'}",
            'reference': payment.receipt_number or '-',
            'amount': payment.amount_paid,
        })
    for req in requests:
        all_activities.append({
            'id': req.id,
            'type': 'Rental',
            'date': req.request_date,
            'category': req.status,
            'description': f"{req.name} rented {req.suit.suit_name if req.suit else 'a suit'} ({req.quantity_requested} pcs)",
            'reference': f"REQ-{req.id}",
            'amount': req.total_amount,
        })

    all_activities = sorted(all_activities, key=lambda item: item['date'], reverse=True)

    context = {
        'total_suits': total_suits,
        'total_quantity': total_quantity,
        'available_quantity': available_quantity,
        'rented_quantity': rented_quantity,
        'total_requests': total_requests,
        'pending_requests': pending_requests,
        'approved_requests': approved_requests,
        'active_requests': active_requests,
        'returned_requests': returned_requests,
        'rejected_requests': rejected_requests,
        'expired_requests': expired_requests,
        'revenue': revenue,
        'total_expenses': total_expenses,
        'dry_cleaning_expenses': dry_cleaning_expenses,
        'other_expenses': total_expenses - dry_cleaning_expenses,
        'net_profit': net_profit,
        'net_profit_before_cleaning': net_profit_before_cleaning,
        'utilization_rate': utilization_rate,
        'recent_requests': filtered_requests[:15],
        'selected_date': selected_date,
        'selected_month': selected_month,
        'selected_recent_month': selected_recent_month,
        'selected_period': selected_period,
        'month_range': month_range,
        'from_date': from_date_str,
        'to_date': to_date_str,
        'date_range_error': date_range_error,
        'now': timezone.now(),
        'luxury_count': luxury_count,
        'standard_count': standard_count,
        'budget_count': budget_count,
        'luxury_units': luxury_units,
        'standard_units': standard_units,
        'budget_units': budget_units,
        'luxury_mix_pct': luxury_mix_pct,
        'standard_mix_pct': standard_mix_pct,
        'budget_mix_pct': budget_mix_pct,
        'revenue_potential': revenue_potential,
        'payment_count': payment_count,
        'expense_count': expense_count,
        'all_activities': all_activities,
        'staff_list': staff_list,
    }
    
    return render(request, "admin/reports.html", context)


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
    if selected_date:
        parsed_date = parse_date(selected_date)
        if parsed_date:
            filter_start = timezone.make_aware(datetime.combine(parsed_date, time.min), timezone.get_current_timezone())
            filter_end = timezone.make_aware(datetime.combine(parsed_date, time.max), timezone.get_current_timezone())
    elif selected_month:
        try:
            year_str, month_str = selected_month.split('-')
            year = int(year_str)
            month = int(month_str)
            filter_start, filter_end = get_month_range(year, month)
        except (ValueError, TypeError):
            selected_month = ''
    elif selected_recent_month:
        try:
            recent_months = int(selected_recent_month)
            year = now.year
            month = now.month - recent_months + 1
            while month <= 0:
                month += 12
                year -= 1
            filter_start, _ = get_month_range(year, month)
            filter_end = now
        except (ValueError, TypeError):
            selected_recent_month = ''
    elif month_range == 'current':
        # Filter 12th-25th of current month
        filter_start, filter_end = get_month_range_12_25(now.year, now.month)
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

    # Date range parameters (no restrictions)
    from_date_str = request.GET.get('from_date', '').strip()
    to_date_str = request.GET.get('to_date', '').strip()
    date_range_error = None
    
    filter_start = None
    filter_end = None
    
    # Parse date range if provided
    if from_date_str or to_date_str:
        try:
            if from_date_str:
                parsed_from = parse_date(from_date_str)
                if parsed_from:
                    filter_start = timezone.make_aware(datetime.combine(parsed_from, time.min), timezone.get_current_timezone())
            if to_date_str:
                parsed_to = parse_date(to_date_str)
                if parsed_to:
                    filter_end = timezone.make_aware(datetime.combine(parsed_to, time.max), timezone.get_current_timezone())
            # Validate date range
            if filter_start and filter_end and filter_start > filter_end:
                date_range_error = "From date must be earlier than or equal to To date."
        except Exception as e:
            date_range_error = f"Invalid date format: {str(e)}"

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

    revenue_agg = SuitRequest.objects.filter(status__in=["Active", "Returned"]).aggregate(total=Sum('total_amount'))
    total_revenue = revenue_agg.get('total') or Decimal('0')

    total_expenses = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    dry_cleaning_expenses = DryCleaning.objects.filter(status='approved').aggregate(total=Sum('amount'))['total'] or Decimal('0')
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
    date_str = request.POST.get("date")
    description = request.POST.get("description", "").strip()
    assigned_user_id = request.POST.get("assigned_user")
    assigned_staff_id = request.POST.get("assigned_staff")

    if not amount or not description:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Please enter expense amount and description.'}, status=400)
        messages.error(request, "Please enter expense amount and description.")
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
    if date_str:
        parsed_date = parse_date(date_str)
        if parsed_date:
            expense_date = timezone.make_aware(datetime.combine(parsed_date, datetime.min.time()))

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
        return redirect("reception_reports")
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
            return redirect(request.META.get('HTTP_REFERER') or 'reception_payments')

        if amount <= 0:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Payment amount must be greater than zero.'}, status=400)
            messages.error(request, "Payment amount must be greater than zero.")
            return redirect(request.META.get('HTTP_REFERER') or 'reception_payments')

        # Do not allow paying more than the balance due
        balance = payment.rental.balance_due or Decimal('0.00')
        if amount > balance:
            if is_ajax:
                return JsonResponse({'success': False, 'message': f'Amount exceeds remaining balance (${balance}).'}, status=400)
            messages.error(request, f"Amount exceeds remaining balance (${balance}).")
            return redirect(request.META.get('HTTP_REFERER') or 'reception_payments')

        payment_method = request.POST.get('payment_method', '').strip() or (payment.payment_method or 'Cash')

        # Parse payment date (date input expected)
        if payment_date_raw:
            parsed_date = parse_date(payment_date_raw)
            if parsed_date:
                payment_dt = timezone.make_aware(datetime.combine(parsed_date, time.min))
            else:
                payment_dt = timezone.now()
        else:
            payment_dt = timezone.now()

        # Past payment dates are allowed for record accuracy, no server-side past-date rejection.
        try:
            payment_date_only = timezone.localtime(payment_dt).date()
        except Exception:
            messages.error(request, "Invalid payment date.")
            return redirect(request.META.get('HTTP_REFERER') or 'reception_payments')

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
        return redirect(request.META.get('HTTP_REFERER') or 'reception_payments')

    # Non-POST fallback
    # Non-POST fallback
    if is_ajax:
        return JsonResponse({'success': False, 'message': f'Use the payment form to record an amount for Payment #{payment.id}.'}, status=400)
    messages.info(request, f"Use the payment form to record an amount for Payment #{payment.id}.")
    return redirect("reception_payments")


@login_required
def record_payment(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        return redirect("reception_payments")

    rental_id = request.POST.get("rental_id")
    amount = request.POST.get("amount")
    method = request.POST.get("method")
    reference = request.POST.get("reference")

    if not rental_id:
        messages.error(request, "Please select a rental to record payment.")
        return redirect("reception_payments")

    req = get_object_or_404(SuitRequest, id=rental_id)

    if req.status not in ["Active", "Returned", "Approved"]:
        messages.error(request, "Selected rental is not eligible for manual payment.")
        return redirect("reception_payments")

    try:
        amount_value = Decimal(amount)
    except (TypeError, ValueError, InvalidOperation):
        messages.error(request, "Please enter a valid payment amount.")
        return redirect("reception_payments")

    if amount_value <= 0:
        messages.error(request, "Payment amount must be greater than zero.")
        return redirect("reception_payments")

    balance_due = req.balance_due
    if balance_due <= Decimal('0.00'):
        messages.error(request, "This rental is already fully paid.")
        return redirect("reception_payments")

    if amount_value > balance_due:
        messages.error(request, "Amount paid cannot exceed the remaining balance.")
        return redirect("reception_payments")

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
    return redirect("reception_payments")


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
    """Admin dry cleaning page - shows items awaiting payment confirmation.
    
    Reception has marked items as paid, admin reviews and confirms payment here.
    Admin can approve, reject, or view approved/rejected items.
    """
    if request.method == 'POST':
        action = request.POST.get('action')
        cleaning_id = request.POST.get('cleaning_id')
        
        if not cleaning_id:
            messages.error(request, 'Laguma helin item-ka.')
            return redirect('dry_cleaning')
        
        dry_clean_record = get_object_or_404(DryCleaning, id=cleaning_id)
        
        if action == 'approve_payment':
            dry_clean_record.paid = True
            dry_clean_record.status = 'approved'
            dry_clean_record.save(update_fields=['paid', 'status'])
            messages.success(request, f"✓ Payment approved for '{dry_clean_record.suit.suit_name}'. Reception can now mark it ready.")

        elif action == 'reject_payment':
            dry_clean_record.status = 'rejected'
            dry_clean_record.paid = False
            dry_clean_record.save(update_fields=['status', 'paid'])
            messages.warning(request, f"✗ '{dry_clean_record.suit.suit_name}' has been rejected. It will stay in the rejected section and cannot be resubmitted.")
        
        return redirect('dry_cleaning')
    
    # Get all DryCleaning records by status
    pending_cleanings = DryCleaning.objects.filter(status='pending').select_related('suit').order_by('-created_at')
    approved_cleanings = DryCleaning.objects.filter(status='approved').select_related('suit').order_by('-created_at')
    rejected_cleanings = DryCleaning.objects.filter(status='rejected').select_related('suit').order_by('-created_at')
    
    context = {
        'laundry_items': pending_cleanings,  # Items awaiting admin review
        'approved_cleanings': approved_cleanings,  # Admin approved items
        'rejected_cleanings': rejected_cleanings,  # Admin rejected items
        'pending_count': pending_cleanings.count(),
        'approved_count': approved_cleanings.count(),
        'rejected_count': rejected_cleanings.count(),
        'total_pending_amount': sum(c.amount for c in pending_cleanings),
    }
    return render(request, 'admin/dry_cleaning.html', context)


@login_required
def mark_dry_cleaning_paid(request, entry_id):
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

        # Create expense record
        Expense.objects.create(
            description=f"Cleaning for {suit.suit_name} (#{suit.id})",
            amount=amount,
            created_by=request.user,
            notes=laundry_notes,
        )

        # Mark suit as available
        suit.status = 'Available'
        suit.save(update_fields=['status'])

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
    returned_requests = SuitRequest.objects.filter(status='Returned').select_related('suit').order_by('-return_date')
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
    """Handle the dry cleaning workflow.
    
    Step 1 (Reception): Mark as Paid → creates DryCleaning record (status='pending'), redirects to laundry page
    Step 2 (Admin): Review payment on admin page and approve (status='approved') or reject (status='rejected')
    Step 3 (Reception): Once admin approves, can click "Mark Ready" to finalize
    
    Rejected items: Don't appear in reception queue again (status='rejected')
    """
    if request.method == 'POST':
        action = request.POST.get('action', 'mark_paid')
        suit_id = request.POST.get('suit_id')
        request_id = request.POST.get('request_id')
        cleaning_cost = request.POST.get('cleaning_cost', '0')
        laundry_notes = request.POST.get('laundry_notes', '')

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

        # Step 1: Reception marks as paid (creates record for admin review)
        if action == 'mark_paid':
            try:
                amount = Decimal(cleaning_cost)
            except (InvalidOperation, TypeError, ValueError):
                messages.error(request, "Fadlan geli lacag sax ah oo nadiifinta ah.")
                return redirect('laundry_backlog')

            existing = DryCleaning.objects.filter(suit_id=suit_request.suit.id).order_by('-created_at').first()
            if existing and existing.status == 'rejected':
                messages.warning(request, f"'{suit_request.suit.suit_name}' was rejected by admin and cannot be submitted again.")
                return redirect('laundry_backlog')

            if existing and existing.status in ['pending', 'approved']:
                messages.info(request, f"'{suit_request.suit.suit_name}' is already in the queue.")
                return redirect('laundry_backlog')

            DryCleaning.objects.create(
                suit_id=suit_request.suit.id,
                amount=amount,
                paid=False,
                status='pending',
                notes=laundry_notes or "Pending admin payment confirmation"
            )
            messages.success(request, f"✓ '{suit_request.suit.suit_name}' marked for payment review. Awaiting admin confirmation.")
            return redirect('laundry_backlog')

        # Step 3: Reception finalizes as ready (only after admin approves payment)
        elif action == 'mark_ready':
            dry_clean = DryCleaning.objects.filter(
                suit_id=suit_request.suit.id,
                status='approved'
            ).order_by('-created_at').first()

            if not dry_clean:
                messages.error(request, f"Admin payment confirmation pending for '{suit_request.suit.suit_name}'. Please wait.")
                return redirect('laundry_backlog')

            with transaction.atomic():
                try:
                    amount = Decimal(cleaning_cost) if cleaning_cost not in [None, ''] else dry_clean.amount
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

                suit = suit_request.suit
                suit.quantity += suit_request.quantity_requested
                suit.status = 'Available'
                suit.save(update_fields=['quantity', 'status'])
                suit.refresh_from_db()

                suit_request.status = 'Returned'
                suit_request.save(update_fields=['status'])
                suit_request.refresh_from_db()

                dry_clean.paid = True
                dry_clean.status = 'approved'
                dry_clean.save(update_fields=['paid', 'status'])
                dry_clean.refresh_from_db()

                messages.success(request, f"🧼 {suit.suit_name} waa laga soo saaray dhar-dhaqista, kharashkiisana waa la diiwaangeliyey!")

            return redirect('laundry_backlog')

    active_tab = request.GET.get('tab', 'all').lower()
    all_items = SuitRequest.objects.filter(status='Returned').select_related('suit').prefetch_related('suit__dry_cleanings')

    filtered_items = []
    for item in all_items:
        suit = item.suit
        if suit.status == 'Available':
            continue

        dry_clean = suit.dry_cleanings.order_by('-created_at').first()
        if not dry_clean:
            filtered_items.append(item)
            continue
        if dry_clean.status == 'rejected':
            continue
        if dry_clean.paid is True and dry_clean.status == 'approved':
            if active_tab == 'pending':
                continue
            if active_tab == 'approved':
                filtered_items.append(item)
                continue
        if active_tab == 'pending' and dry_clean.paid is True:
            continue
        if active_tab == 'approved' and not (dry_clean.paid is True and dry_clean.status == 'approved'):
            continue
        filtered_items.append(item)

    page_number = request.GET.get('page', 1)
    paginator = Paginator(filtered_items, 10)
    page_obj = paginator.get_page(page_number)

    return render(request, 'reception/laundry_page.html', {
        'laundry_items': page_obj.object_list,
        'page_obj': page_obj,
        'active_tab': active_tab,
    })


@login_required
def apply_discount(request, rental_id=None):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        messages.error(request, "Discount must be submitted via POST.")
        return redirect("reception_payments")

    if rental_id is None:
        rental_id = request.POST.get("rental_id")

    if not rental_id:
        messages.error(request, "No rental was specified for discount.")
        return redirect("reception_payments")

    try:
        rental_id_int = int(rental_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid rental identifier provided.")
        return redirect("reception_payments")

    rental = SuitRequest.objects.filter(id=rental_id_int).first()
    if not rental:
        messages.error(request, f"Rental #{rental_id} was not found.")
        return redirect("reception_payments")

    discount_type = request.POST.get("discount_type", "fixed")
    discount_value = Decimal(request.POST.get("discount_value", "0"))
    discount_reason = request.POST.get("discount_reason", "")

    if discount_value <= 0:
        messages.error(request, "Discount value must be greater than zero.")
        return redirect("reception_payments")

    if discount_type == "percentage":
        if discount_value > 100:
            messages.error(request, "Percentage discount cannot exceed 100%.")
            return redirect("reception_payments")
        discount_amount = rental.total_amount * (discount_value / Decimal(100))
    else:
        if discount_value > rental.total_amount:
            messages.error(request, "Fixed discount cannot exceed total rental amount.")
            return redirect("reception_payments")
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
    return redirect("reception_payments")


@login_required
def process_refund(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    if request.method != "POST":
        messages.error(request, "Refund must be submitted via POST.")
        return redirect("reception_payments")

    payment_id = request.POST.get("payment_id")
    if not payment_id:
        messages.error(request, "No payment was specified for refund.")
        return redirect("reception_payments")

    # Validate payment_id is an integer and that the Payment exists.
    try:
        payment_id_int = int(payment_id)
    except (TypeError, ValueError):
        messages.error(request, "Invalid payment identifier provided.")
        return redirect("reception_payments")

    payment = Payment.objects.filter(id=payment_id_int).first()
    if not payment:
        messages.error(request, f"Payment #{payment_id} was not found.")
        return redirect("reception_payments")
    refund_amount = Decimal(request.POST.get("refund_amount", "0"))
    if refund_amount <= 0 or refund_amount > payment.amount_paid:
        messages.error(request, "Refund amount must be greater than zero and not exceed the original payment.")
        return redirect("reception_payments")

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
    return redirect("reception_payments")


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
            req.suit.suit_name,
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
    if not is_reception_or_cashier_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    report_type = request.GET.get('type', 'rentals').lower()
    selected_period = request.GET.get('period', 'all').strip().lower()
    from_date_str = request.GET.get('from_date', '').strip()
    to_date_str = request.GET.get('to_date', '').strip()
    now = timezone.localtime()

    def get_month_range_12_25(year, month):
        """Get date range from 12th to 25th of a month"""
        start_date = datetime(year, month, 12, 0, 0, 0)
        end_date = datetime(year, month, 25, 23, 59, 59, 999999)
        tz = timezone.get_current_timezone()
        return timezone.make_aware(start_date, tz), timezone.make_aware(end_date, tz)

    filter_start = None
    filter_end = None
    
    # Priority: use new from_date/to_date if provided
    if from_date_str or to_date_str:
        try:
            if from_date_str:
                parsed_from = parse_date(from_date_str)
                if parsed_from:
                    filter_start = timezone.make_aware(datetime.combine(parsed_from, time.min), timezone.get_current_timezone())
            if to_date_str:
                parsed_to = parse_date(to_date_str)
                if parsed_to:
                    filter_end = timezone.make_aware(datetime.combine(parsed_to, time.max), timezone.get_current_timezone())
        except Exception:
            pass
    
    # Fallback to period filtering
    if not filter_start and not filter_end:
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
        else:
            # Default to all data
            filter_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    response = HttpResponse(content_type='text/csv')
    writer = csv.writer(response)

    if report_type == 'expenses':
        response['Content-Disposition'] = 'attachment; filename="expenses_report.csv"'
        writer.writerow(['Date', 'Category', 'Description', 'Amount', 'Created By', 'Notes'])
        
        expenses = Expense.objects.order_by('-expense_date')
        if filter_start and filter_end:
            expenses = expenses.filter(expense_date__range=(filter_start, filter_end))
        elif filter_start:
            expenses = expenses.filter(expense_date__gte=filter_start)
        elif filter_end:
            expenses = expenses.filter(expense_date__lte=filter_end)
        
        # Add expense details
        for exp in expenses:
            writer.writerow([
                exp.expense_date.strftime('%Y-%m-%d %H:%M') if exp.expense_date else '',
                exp.notes or 'Other',
                exp.description or '',
                f"${exp.amount:.2f}",
                exp.created_by.username if exp.created_by else 'Unknown',
                exp.notes or '',
            ])
        
        # Add summary row
        total_expenses = sum(e.amount for e in expenses)
        writer.writerow([])  # Empty row
        writer.writerow(['TOTAL EXPENSES', '', '', f"${total_expenses:.2f}", '', ''])
    else:
        # Default: export rentals
        response['Content-Disposition'] = 'attachment; filename="financial_report.csv"'
        writer.writerow(['Date', 'Customer', 'Suit', 'Quantity', 'Total Amount', 'Paid Amount', 'Balance Due', 'Status', 'Payment Status'])

        rentals = SuitRequest.objects.order_by('-request_date')
        if filter_start and filter_end:
            rentals = rentals.filter(request_date__range=(filter_start, filter_end))
        elif filter_start:
            rentals = rentals.filter(request_date__gte=filter_start)
        elif filter_end:
            rentals = rentals.filter(request_date__lte=filter_end)

        for req in rentals:
            paid = req.amount_paid or 0
            balance = req.balance_due or 0
            payment_status = "Paid" if req.is_paid else f"Due: ${balance:.2f}"
            
            writer.writerow([
                req.request_date.strftime('%Y-%m-%d') if req.request_date else '',
                req.name,
                req.suit.suit_name,
                req.quantity_requested or 1,
                f"${req.total_amount:.2f}",
                f"${paid:.2f}",
                f"${balance:.2f}",
                req.status,
                payment_status,
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
    return redirect("reception_payments")


@login_required
def daily_report(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied.")
        return redirect("home")

    messages.info(request, "Daily reporting is not yet implemented.")
    return redirect("reception_payments")


@login_required
def close_shift(request):
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")

    messages.success(request, "Shift closed successfully.")
    return redirect("reception_payments")


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
    if not is_admin(request.user):
        messages.error(request, "Access denied. Admin only.")
        return redirect("home")
    
    from django.contrib.auth.models import User
    users = User.objects.all().order_by('-date_joined')
    for u in users:
        Profile.objects.get_or_create(user=u)
    
    current_q = request.GET.get("q", "").strip()
    if current_q:
        users = users.filter(
            Q(username__icontains=current_q) |
            Q(email__icontains=current_q)
        )
    
    total_users = User.objects.count()
    staff_count = User.objects.filter(is_staff=True).count()
    customer_count = total_users - staff_count
    # Include Staff model entries (custom staff records)
    try:
        from .models import Staff
        staff_list = Staff.objects.all().order_by('-hire_date')
        staff_total = Staff.objects.count()
        now = timezone.localtime()
        for staff in staff_list:
            staff.total_paid = staff.payrolls.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            staff.total_advance = staff.total_advances()
            staff.monthly_paid = staff.total_paid_for_month(now.month, now.year)
            staff.remaining_this_month = staff.remaining_salary_for_month(now.month, now.year)
            staff.payrolls_history = staff.payrolls.order_by('-payment_date')
    except Exception:
        staff_list = []
        staff_total = 0
    
    context = {
        "users": users,
        "current_q": current_q,
        "total_users": total_users,
        "staff_count": staff_count,
        "active_users": User.objects.filter(is_active=True).count(),
        "customer_count": customer_count,
        'staff_list': staff_list,
        'staff_total': staff_total,
    }
    
    return render(request, "admin/manage_users.html", context)


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

        hire_date = parse_date(hire_date_raw) if hire_date_raw else None

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

        payment_date = parse_date(payment_date_str)
        if not payment_date:
            messages.error(request, "Enter a valid payment date.")
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
    available_count = Suit.objects.filter(status='Available').count()
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
        except Exception:
            quantity = 1

        price_raw = request.POST.get('price_per_day')
        # Only require price for items that use price (suits/shirts). For others, default to 0.00.
        if price_raw in (None, '') and category not in ['suits', 'shirts']:
            price_per_day = Decimal('0.00')
        else:
            try:
                price_per_day = Decimal(price_raw)
            except (InvalidOperation, TypeError):
                messages.error(request, "Please enter a valid numeric price.")
                return redirect('reception_add_item')

        condition_grade = request.POST.get('condition_grade', 'new')
        damage_notes = request.POST.get('damage_notes', '').strip()

        if not suit_name or not color:
            messages.error(request, "Please fill in all required fields.")
            return redirect('reception_add_item')

        try:
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
            messages.success(request, f"Suit '{suit_name}' has been added successfully!")
        except Exception as e:
            messages.error(request, f"Error adding suit: {str(e)}")
            return redirect('reception_inventory')

        # After creating, redirect to the suit profile so the admin/reception can review details
        try:
            return redirect('suit_profile', suit.id)
        except Exception:
            return redirect('reception_inventory')
    
    return render(request, 'reception/reception_add_item.html')


@login_required
def edit_suit_view(request, suit_id):
    """Edit a suit's details"""
    if not is_reception_or_admin(request.user):
        messages.error(request, "Access denied. Reception/Admin only.")
        return redirect("home")
    
    suit = get_object_or_404(Suit, id=suit_id)
    
    if request.method == 'POST':
        suit.suit_name = request.POST.get('suit_name', suit.suit_name)
        suit.collection = request.POST.get('collection', suit.collection)
        suit.description = request.POST.get('description', suit.description)
        suit.size = request.POST.get('size', suit.size)
        suit.item_type = request.POST.get('category', suit.item_type or 'suits')
        suit.color = request.POST.get('color', suit.color)
        suit.quantity = int(request.POST.get('quantity', suit.quantity))
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
        return redirect('reception_inventory')
    
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
        return redirect('reception_inventory')
    
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
        
        return redirect('reception_inventory')
    
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
        back_url = reverse('reception_inventory')
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
    """Mark a suit as being sent to dry cleaning (Returned -> Dry Cleaning)"""
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
        return redirect("reception_inventory")
    
    if request.method == 'POST':
        suit.status = "Dry Cleaning"
        suit.save()
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'new_status': suit.status})
        messages.success(request, f"{suit.suit_name} marked as Dry Cleaning.")
        return redirect("reception_inventory")
    
    # For GET requests, just redirect
    return redirect("reception_inventory")


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
        return redirect("reception_inventory")
    
    if request.method == 'POST':
        suit.status = "Available"
        suit.save()
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'new_status': suit.status})
        messages.success(request, f"{suit.suit_name} is now available for rent.")
        return redirect("reception_inventory")
    
    # For GET requests, just redirect
    return redirect("reception_inventory")

