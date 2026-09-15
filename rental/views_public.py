from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache

from .forms import validate_image_upload
from .models import ContactMessage, FavoriteSuit, Suit, SuitRequest


def home(request):
    just_logged_out = False
    if request.session.get("just_logged_out"):
        just_logged_out = True
        del request.session["just_logged_out"]

    suits = Suit.objects.exclude(condition_grade='retired')
    context = {
        "just_logged_out": just_logged_out,
        "total_suits": suits.count(),
        "available_suits": suits.filter(status="Available").count(),
        "luxury_count": suits.filter(collection="Luxury").count(),
        "standard_count": suits.filter(collection="Standard").count(),
        "budget_count": suits.filter(collection="Budget").count(),
    }
    return render(request, "rental/home.html", context)


def service_page(request):
    return render(request, "rental/service.html")


def contact_page(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        subject = request.POST.get("subject", "").strip()
        message = request.POST.get("message", "").strip()

        if not name or not email or not message:
            messages.error(request, "Please fill in your name, email, and message.")
            return redirect("contact_page")

        if "@" not in email or "." not in email.split("@")[-1]:
            messages.error(request, "Please enter a valid email address.")
            return redirect("contact_page")

        if len(message) < 10:
            messages.error(request, "Please write a little more detail so we can help you.")
            return redirect("contact_page")

        ContactMessage.objects.create(
            name=name,
            email=email,
            phone=phone,
            subject=subject or "General Inquiry",
            message=message,
        )
        messages.success(request, "Your message has been saved. We will contact you soon.")
        return redirect("contact_page")

    return render(request, "rental/contact.html")


def about_page(request):
    return render(request, "rental/about.html")


def all_suits(request):
    just_logged_out = False
    if request.session.get("just_logged_out"):
        just_logged_out = True
        del request.session["just_logged_out"]
    suits = Suit.objects.exclude(condition_grade='retired').order_by("-id")

    if request.GET.get("collection"):
        suits = suits.filter(collection=request.GET.get("collection"))

    selected_category = request.GET.get("category", "all").lower()
    if selected_category != "all":
        category_keywords = {
            "suits": ["suit", "suits"],
            "shirts": ["shirt", "shirts"],
            "shoes": ["shoe", "shoes"],
            "ties": ["tie", "ties"],
            "belts": ["belt", "belts"],
            "accessories": ["tie", "ties", "belt", "belts", "shoe", "shoes"],
        }
        keywords = category_keywords.get(selected_category, [])
        if keywords:
            category_filter = Q()
            for keyword in keywords:
                category_filter |= Q(suit_name__icontains=keyword) | Q(description__icontains=keyword)
            suits = suits.filter(category_filter)

    if request.GET.get("q"):
        suits = suits.filter(suit_name__icontains=request.GET.get("q"))

    selected_collection = request.GET.get("collection", "")

    all_items_count = Suit.objects.count()
    suits_count = Suit.objects.filter(
        Q(suit_name__icontains="suit") | Q(description__icontains="suit")
    ).count()
    shirts_count = Suit.objects.filter(
        Q(suit_name__icontains="shirt") | Q(description__icontains="shirt")
    ).count()
    accessories_count = Suit.objects.filter(
        Q(suit_name__icontains="tie") | Q(description__icontains="tie") |
        Q(suit_name__icontains="belt") | Q(description__icontains="belt") |
        Q(suit_name__icontains="shoe") | Q(description__icontains="shoe")
    ).count()
    luxury_count = Suit.objects.filter(collection="Luxury").count()
    standard_count = Suit.objects.filter(collection="Standard").count()
    budget_count = Suit.objects.filter(collection="Budget").count()

    user_favorites = []
    if request.user.is_authenticated:
        user_favorites = list(FavoriteSuit.objects.filter(user=request.user).values_list("suit_id", flat=True))

    return render(request, "admin/all_suits.html", {
        "suits": suits,
        "just_logged_out": just_logged_out,
        "user_favorites": user_favorites,
        "selected_category": selected_category,
        "selected_collection": selected_collection,
        "all_items_count": all_items_count,
        "suits_count": suits_count,
        "shirts_count": shirts_count,
        "accessories_count": accessories_count,
        "luxury_count": luxury_count,
        "standard_count": standard_count,
        "budget_count": budget_count,
    })


@login_required(login_url="customer_login")
def book_suit(request):
    from .views import is_admin, validate_phone_number

    if request.method == "POST":
        suit_id_raw = request.POST.get("suit_id", "").strip()
        if not suit_id_raw:
            messages.error(request, "Please select a suit before submitting your booking.")
            return redirect("all_suits")

        try:
            suit_id = int(suit_id_raw)
        except (TypeError, ValueError):
            messages.error(request, "Please select a valid suit before submitting your booking.")
            return redirect("all_suits")

        suit = get_object_or_404(Suit.objects.exclude(condition_grade='retired'), id=suit_id)
        quantity_requested_raw = (request.POST.get("quantity_requested") or request.POST.get("quantity") or "1").strip()
        days_requested_raw = (request.POST.get("days_requested") or request.POST.get("days") or "1").strip()

        try:
            quantity_requested = int(quantity_requested_raw)
            days_requested = int(days_requested_raw)
        except ValueError:
            messages.error(request, "Please enter valid numeric values for quantity and rental days.")
            return redirect("all_suits")

        if quantity_requested < 1 or quantity_requested > 5:
            messages.error(request, "You can request between 1 and 5 suits.")
            return redirect("all_suits")

        if quantity_requested > suit.quantity:
            messages.error(request, f"Only {suit.quantity} suits available. Maximum reached.")
            return redirect("all_suits")

        if not suit.is_available_for_booking:
            messages.error(request, "This suit is not available.")
            return redirect("all_suits")

        phone = (request.POST.get("phone") or request.POST.get("phone_number") or "").strip()
        try:
            validate_phone_number(phone)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("all_suits")

        full_name = (request.POST.get("full_name") or request.POST.get("name") or request.POST.get("first_name") or "").strip()
        first_name = (request.POST.get("first_name") or full_name or "").strip()
        second_name = (request.POST.get("second_name") or request.POST.get("last_name") or "").strip()
        submitted_email = (request.POST.get("email") or "").strip()
        customer_name = (
            f"{first_name} {second_name}".strip()
            or full_name
            or (request.user.get_full_name().strip() if request.user.get_full_name().strip() else request.user.username)
        )
        address_value = (request.POST.get("present_address") or request.POST.get("address") or "").strip()

        SuitRequest.objects.create(
            suit=suit,
            first_name=first_name or None,
            second_name=second_name or None,
            name=customer_name,
            phone=phone,
            customer_email=submitted_email or (request.user.email if request.user else None),
            present_address=address_value or None,
            parents_address=request.POST.get("parents_address") or None,
            notes=request.POST.get("notes") or None,
            days_requested=days_requested,
            quantity_requested=quantity_requested,
            customer_image=request.FILES.get("customer_image") or request.FILES.get("image"),
            user=request.user,
            status="Pending",
        )

        suit.status = "Pending Request"
        suit.save()

        messages.success(request, f"Request for {quantity_requested} x {suit.suit_name} submitted. Admin will approve it soon.")
        return redirect("customer_history")

    return redirect("all_suits")


@login_required(login_url="customer_login")
def request_suit(request, suit_id):
    from .views import is_admin, validate_national_id, validate_phone_number

    suit = get_object_or_404(Suit.objects.exclude(condition_grade='retired'), id=suit_id)

    if request.method == "POST":
        quantity_requested_raw = (request.POST.get("quantity_requested") or request.POST.get("quantity") or "1").strip()
        days_requested_raw = (request.POST.get("days_requested") or request.POST.get("days") or "1").strip()

        try:
            quantity_requested = int(quantity_requested_raw)
            days_requested = int(days_requested_raw)
        except ValueError:
            messages.error(request, "Please enter valid numeric values for quantity and rental days.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        if quantity_requested < 1 or quantity_requested > 5:
            messages.error(request, "You can request between 1 and 5 suits.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        if quantity_requested > suit.quantity:
            messages.error(request, f"Only {suit.quantity} suits available. Maximum reached.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        if not suit.is_available_for_booking:
            messages.error(request, "Suit not available.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        full_name_value = (request.POST.get("full_name") or request.POST.get("name") or request.POST.get("first_name") or "").strip()
        first_name_value = (request.POST.get("first_name") or full_name_value or "").strip()
        second_name_value = (request.POST.get("second_name") or request.POST.get("last_name") or "").strip()
        if not full_name_value and first_name_value and second_name_value:
            full_name_value = f"{first_name_value} {second_name_value}".strip()
        if not full_name_value:
            messages.error(request, "Please enter your full name.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        phone_value = (request.POST.get("phone") or request.POST.get("phone_number") or "").strip()
        try:
            validate_phone_number(phone_value)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        address_value = (request.POST.get("present_address") or request.POST.get("address") or "").strip()
        if not address_value:
            messages.error(request, "Please enter a delivery address.")
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        submitted_email = (request.POST.get("email") or "").strip()

        national_id_value = request.POST.get("national_id") or ""
        try:
            national_id_value = validate_national_id(national_id_value)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        image_file = request.FILES.get("customer_image") or request.FILES.get("image")
        if image_file:
            try:
                if not is_admin(request.user):
                    validate_image_upload(image_file, "customer image")
            except forms.ValidationError as e:
                messages.error(request, str(e))
                return render(request, "customer/request_form.html", {"suit": suit, "form_values": request.POST})

        SuitRequest.objects.create(
            suit=suit,
            first_name=first_name_value or None,
            second_name=second_name_value or None,
            name=full_name_value or (request.POST.get("name") or ""),
            phone=phone_value,
            customer_email=submitted_email or (request.user.email if request.user else None),
            national_id=national_id_value or None,
            present_address=address_value,
            parents_address=request.POST.get("parents_address") or None,
            notes=request.POST.get("notes") or None,
            days_requested=days_requested,
            quantity_requested=quantity_requested,
            customer_image=image_file,
            user=request.user,
            status="Pending",
        )

        messages.success(request, "Your suit request has been submitted for admin approval.")
        return redirect("customer_history")

    return render(request, "customer/request_form.html", {"suit": suit, "form_values": {}})


def customer_login(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_active:
            login(request, user)
            request.session.set_expiry(28800)
            messages.success(request, "Login successful!")
            return redirect("all_suits")
        messages.error(request, "Invalid username or password")
        return redirect("customer_login")
    return render(request, "customer/customer_login.html")


@never_cache
@login_required(login_url="customer_login")
def customer_history(request):
    bookings = SuitRequest.objects.filter(user=request.user).order_by("-request_date")
    return render(request, "customer/customer_history.html", {"bookings": bookings})


def login_view(request):
    from .views import is_admin, is_cashier, is_reception

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_active and (is_reception(user) or is_cashier(user) or is_admin(user)):
            login(request, user)
            if is_reception(user) or is_cashier(user):
                messages.success(request, "Reception login successful! Welcome to your reception dashboard.")
                return redirect("reception_dashboard")
            messages.success(request, "Admin login successful! Welcome to your admin dashboard.")
            return redirect("dashboard")
        if user is not None and user.is_active and not (is_reception(user) or is_cashier(user) or is_admin(user)):
            messages.error(request, "This account is not allowed on the reception/admin login page.")
        else:
            messages.error(request, "Invalid username or password.")
        return redirect("admin_login")
    return render(request, "rental/login.html")


def customer_logout(request):
    user_is_staff = getattr(request.user, "is_staff", False)
    logout(request)
    request.session.flush()
    request.session["just_logged_out"] = True
    response = redirect("home")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def logout_view(request):
    user_is_staff = getattr(request.user, "is_staff", False)
    logout(request)
    request.session.flush()
    request.session["just_logged_out"] = True
    response = redirect("home")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response
