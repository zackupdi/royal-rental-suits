from django.db import models
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.templatetags.static import static
from datetime import timedelta
from math import ceil
import datetime
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal, ROUND_HALF_UP
from .storage import private_media_storage


class Suit(models.Model):
    COLLECTION_CHOICES = [
        ("Luxury", "Luxury"),
        ("Standard", "Standard"),
        ("Budget", "Budget"),
    ]

    STATUS_CHOICES = [
        ("Available", "Available"),
        ("Pending Request", "Pending Request"),
        ("Reserved", "Reserved"),
        ("Rented", "Rented"),
        ("Returned", "Returned"),
        ("Dry Cleaning", "Dry Cleaning"),
    ]

    CONDITION_CHOICES = [
        ('new', 'Cusub (New)'),
        ('medium', 'Dhexe (Medium)'),
        ('retired', 'Aad u Raagay (Retired/In-active)'),
    ]

    suit_name = models.CharField(max_length=100)
    price_per_day = models.DecimalField(max_digits=10, decimal_places=2)

    collection = models.CharField(
        max_length=20,
        choices=COLLECTION_CHOICES,
        default="Standard"
    )

    size = models.CharField(max_length=20, default="M")
    item_type = models.CharField(max_length=20, default="suits", blank=True)
    color = models.CharField(max_length=30, default="Black")
    description = models.TextField(blank=True, null=True)

    quantity = models.PositiveIntegerField(default=1)  # Inventory quantity

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="Available"
    )

    # Darajooyinka Suit-ka (Condition Grades)
    condition_grade = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='new')

    # Cilladaha iyo Dhaawacyada (Damage notes)
    damage_notes = models.TextField(blank=True, null=True, help_text="Halkan ku qor haddii uu leeyahay jeexitaan, buraash, iwd.")

    # Xogta Maaliyadda iyo Caannimada (Financial & rental tracking)
    total_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    rental_count = models.PositiveIntegerField(default=0)

    rental_end_time = models.DateTimeField(null=True, blank=True)
    rented_at = models.DateTimeField(blank=True, null=True)  # When suit was rented
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    image = models.ImageField(upload_to="suits/", null=True, blank=True)

    @property
    def image_url(self):
        if self.image:
            return self.image.url

        item_details = " ".join(
            filter(None, [self.item_type, self.suit_name, self.description])
        ).lower()
        fallback_images = (
            ("belt", "images/accessories/leather-belt.svg"),
            ("shoe", "images/accessories/dress-shoes.svg"),
            ("tie", "images/accessories/formal-tie.svg"),
            ("shirt", "images/accessories/formal-shirt.svg"),
        )
        for keyword, image_path in fallback_images:
            if keyword in item_details:
                return static(image_path)
        return static("luxury1.jpg")

    @property
    def is_retired(self):
        return self.condition_grade == 'retired'

    @property
    def category(self):
        item_type = (self.item_type or "").strip().lower()
        if item_type in {"shirts"}:
            return "shirts"
        if item_type in {"ties", "shoes", "belts"}:
            return "accessories"
        if item_type in {"suits"}:
            return "suits"

        text = " ".join(filter(None, [self.suit_name, self.description])).lower()
        if any(term in text for term in ["shirt", "shirts", "t-shirt", "tshirts", "polo"]):
            return "shirts"
        if any(term in text for term in ["tie", "ties", "belt", "belts", "shoe", "shoes", "accessory", "accessories", "vest", "cufflink", "cufflinks", "suspenders", "sock", "socks"]):
            return "accessories"
        return "suits"

    @property
    def category_display(self):
        return self.category.title() if self.category else "Suits"

    @property
    def is_available_soon(self):
        next_available = self.next_available_time
        return bool(next_available and next_available <= timezone.now())

    @property
    def should_queue_for_dry_cleaning(self):
        item_type = (self.item_type or "").strip().lower()
        if item_type in {"ties", "shoes", "belts"}:
            return False
        if item_type in {"suits", "shirts"}:
            return True
        return self.category != "accessories"

    @property
    def reserved_quantity(self):
        total = self.suitrequest_set.filter(status="Active").aggregate(total=Sum("quantity_requested"))['total']
        return total or 0

    @property
    def total_stock(self):
        return self.quantity + self.reserved_quantity

    @property
    def available_quantity(self):
        return max(self.quantity - self.held_quantity, 0)

    @property
    def held_quantity(self):
        held = self.suitrequest_set.filter(status="Reserved").filter(
            Q(due_date__isnull=True) | Q(due_date__gt=timezone.now())
        ).aggregate(total=Sum("quantity_requested"))["total"]
        return held or 0

    @property
    def next_available_time(self):
        if self.available_quantity > 0:
            return timezone.now()
        active_request = self.suitrequest_set.filter(status="Active", end_time__isnull=False).order_by('end_time').first()
        return active_request.end_time if active_request else None

    @property
    def next_available_text(self):
        next_time = self.next_available_time
        if not next_time:
            return "Unknown"
        return "Now" if next_time <= timezone.now() else next_time.strftime('%d %b %Y %H:%M')

    @property
    def is_available_for_booking(self):
        return not self.is_retired and self.available_quantity > 0

    def refresh_rental_end_time(self):
        active_request = self.suitrequest_set.filter(status="Active", end_time__isnull=False).order_by('end_time').first()
        self.rental_end_time = active_request.end_time if active_request else None
        self.save(update_fields=['rental_end_time'])

    @property
    def is_low_stock(self):
        return self.available_quantity <= 2

    def save(self, *args, **kwargs):
        # Haddii xaaladdu isu beddesho 'rented' oo markaa la kireynayo, dabool saacadda hadda
        if self.status == 'Rented' and not self.rented_at:
            self.rented_at = timezone.now()
        elif self.status != 'Rented':
            self.rented_at = None
        super().save(*args, **kwargs)

    @property
    def hours_remaining(self):
        """Waxay xisaabinaysaa 24 saac inta ka dhiman haddii la kireeyay"""
        if self.status == 'Rented' and self.rented_at:
            elapsed = timezone.now() - self.rented_at
            remaining = datetime.timedelta(hours=24) - elapsed
            total_seconds = remaining.total_seconds()
            if total_seconds > 0:
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                return f"{hours}h {minutes}m"
            return "Waqtigu waa dhammaaday (24h+)"
        return None

    @property
    def rented_at_timestamp(self):
        """Waxay soo celinaysaa waqtiga la kireeyay oo JavaScript u isticmaali karo"""
        if self.rented_at:
            return int(self.rented_at.timestamp() * 1000)
        return None

    def mark_as_returned(self):
        """Markii suit-ka la soo celiyo, waxay u bedelaysaa 'Returned' oo markaa 'Dry Cleaning'"""
        self.status = 'Returned'
        self.save()

    def mark_as_available(self):
        """Markii suit-ka la nadiifiyo, waxay u bedelaysaa 'Available'"""
        self.status = 'Available'
        self.save()
        self.status = 'Available'
        self.save()

    def add_earnings(self, amount):
        """Waxay ku dareysaa lacagta uu sameeyay"""
        self.total_earnings += amount
        self.rental_count += 1
        self.save()

    def refresh_rental_stats(self):
        """Recalculate rental count and revenue from the related rental requests."""
        requests = self.suitrequest_set.exclude(status__in=["Pending", "Rejected"])
        total_earnings = sum((request.total_amount or Decimal("0.00")) for request in requests)
        self.total_earnings = total_earnings.quantize(Decimal("0.01")) if total_earnings else Decimal("0.00")
        self.rental_count = requests.count()
        self.save(update_fields=["total_earnings", "rental_count"])

    def __str__(self):
        return f"{self.suit_name} ({self.size}) - {self.get_condition_grade_display()}"


class ContactMessage(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, null=True)
    subject = models.CharField(max_length=100, blank=True, null=True)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.subject or 'Contact'}"


class DryCleaning(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Admin Approval'),
        ('approved', 'Approved by Admin'),
        ('rejected', 'Rejected by Admin'),
        ('sent_to_laundry', 'Sent to Laundry'),
        ('in_laundry', 'In Laundry'),
        ('completed', 'Cleaning Completed'),
        ('returned', 'Returned to Inventory'),
    ]
    
    suit = models.ForeignKey('Suit', on_delete=models.CASCADE, related_name='dry_cleanings')
    rental_request = models.ForeignKey('SuitRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='dry_cleanings')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='dry_cleaning_requests')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_dry_cleanings')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    date = models.DateField(default=timezone.now)
    notes = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    requested_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_to_laundry_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)

    @property
    def requested_by_display(self):
        if not self.requested_by:
            return "Staff"
        return self.requested_by.get_full_name() or self.requested_by.username

    @property
    def approved_by_display(self):
        if not self.approved_by:
            return "Admin"
        return self.approved_by.get_full_name() or self.approved_by.username

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.suit} - ${self.amount} ({'Paid' if self.paid else 'Unpaid'})"


class SuitRequest(models.Model):
    DEFAULT_RENTAL_DURATION_MINUTES = 24 * 60
    WORKFLOW_STATUS_CHOICES = [
        ("request", "Codsi"),
        ("pending", "Sugaya"),
        ("waiting", "Hakad / Sugitaan"),
        ("rented", "La kiraystay"),
        ("returned", "La soo celiyay"),
        ("completed", "Dhammaaday"),
        ("cancelled", "La joojiyay"),
        ("expired", "Waqtigii wuu dhacay"),
    ]
    BOOKING_TYPE_CHOICES = [
        ("rental", "Kiro"),
        ("reservation", "Boos sii qabsasho"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    suit = models.ForeignKey(Suit, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    customer_email = models.EmailField(blank=True, null=True)  # Customer's email for notifications
    national_id = models.CharField(max_length=50, blank=True, null=True)
    # Additional customer name/address fields
    first_name = models.CharField(max_length=50, blank=True, null=True)
    second_name = models.CharField(max_length=50, blank=True, null=True)
    present_address = models.CharField(max_length=255, blank=True, null=True)
    parents_address = models.CharField(max_length=255, blank=True, null=True)

    customer_image = models.ImageField(
        upload_to="customers/",
        storage=private_media_storage,
        blank=True,
        null=True,
    )

    request_date = models.DateTimeField(default=timezone.now)

    requested_start_date = models.DateField(null=True, blank=True)
    requested_end_date = models.DateField(null=True, blank=True)
    booking_type = models.CharField(
        max_length=12,
        choices=BOOKING_TYPE_CHOICES,
        default="rental",
    )
    workflow_status = models.CharField(
        max_length=12,
        choices=WORKFLOW_STATUS_CHOICES,
        default="request",
        db_index=True,
    )
    waiting_deadline = models.DateTimeField(null=True, blank=True)

    rental_start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    return_date = models.DateTimeField(null=True, blank=True)
    RETURN_CONDITION_CHOICES = [
        ("good", "Good"),
        ("damaged", "Damaged"),
    ]
    return_condition = models.CharField(
        max_length=20,
        choices=RETURN_CONDITION_CHOICES,
        default="good",
    )
    returned_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="processed_rental_returns",
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    suit_price_per_day = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    days_requested = models.IntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(365)])
    rent_days = models.PositiveIntegerField(default=1)
    approved_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateTimeField(null=True, blank=True)
    quantity_requested = models.IntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(5)])  # Allow up to 5 suits
    notes = models.TextField(blank=True, null=True)
    rejected_reason = models.CharField(max_length=255, blank=True)
    accessory_details = models.JSONField(default=dict, blank=True)
    discount_type = models.CharField(max_length=7, blank=True, default="")
    discount_value = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    guarantee_total = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    guarantee_refunded = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    guarantee_settled_at = models.DateTimeField(null=True, blank=True)

    @property
    def start_time(self):
        return self.rental_start_time

    @start_time.setter
    def start_time(self, value):
        self.rental_start_time = value
    status = models.CharField(
        max_length=20,
        choices=[
            ("Pending", "Pending"),
            ("Approved", "Approved"),
            ("Rejected", "Rejected"),
            ("Active", "Active"),  # Currently rented
            ("Returned", "Returned"),  # Returned after rental
            ("Expired", "Expired"),
        ],
        default="Pending"
    )
    is_notified = models.BooleanField(default=False)
    is_email_sent = models.BooleanField(default=False)
    is_paid = models.BooleanField(default=False)
    payment_method = models.CharField(max_length=50, blank=True, null=True)
    payment_reference = models.CharField(max_length=255, blank=True, null=True)

    @property
    def is_overdue(self):
        due = self.due_date or self.end_time
        return bool(self.status == "Active" and due and timezone.now() > due)

    @property
    def time_remaining(self):
        due = self.due_date or self.end_time
        if self.status == "Active" and due:
            remaining = due - timezone.now()
            return max(remaining, timezone.timedelta(0))
        return timezone.timedelta(0)

    @property
    def days_remaining(self):
        remaining = self.time_remaining
        if remaining <= timezone.timedelta(0):
            return 0
        return max(int(remaining.total_seconds() // 86400), 0)

    @property
    def remaining_time_display(self):
        if self.status != "Active":
            return "Completed"

        remaining = self.time_remaining
        if remaining <= timezone.timedelta(0):
            due = self.due_date or self.end_time
            if due and timezone.now() > due:
                overdue = timezone.now() - due
                hours = int(overdue.total_seconds() // 3600)
                if hours < 24:
                    return f"{hours} hour{'s' if hours != 1 else ''} late"
                days = hours // 24
                rem_hours = hours % 24
                if rem_hours:
                    return f"{days} day{'s' if days != 1 else ''} {rem_hours} hour{'s' if rem_hours != 1 else ''} late"
                return f"{days} day{'s' if days != 1 else ''} late"
            return "Expired"

        total_seconds = int(remaining.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours >= 24:
            days, rem_hours = divmod(hours, 24)
            if rem_hours:
                return f"{days}d {rem_hours}h {minutes:02d}m"
            return f"{days}d"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def time_elapsed(self):
        if self.rental_start_time:
            elapsed = timezone.now() - self.rental_start_time
            return max(elapsed, timezone.timedelta(0))
        return timezone.timedelta(0)

    def _calculate_late_fee(self, actual_return):
        due_time = self.due_date or self.end_time
        if not due_time or not actual_return or actual_return <= due_time:
            return Decimal('0.00')

        overdue_seconds = int((actual_return - due_time).total_seconds())
        if overdue_seconds <= 0:
            return Decimal('0.00')

        policy = RentalPolicy.get_solo()
        overdue_hours = Decimal(overdue_seconds) / Decimal(3600)
        if policy.partial_hour_policy == "round_up":
            overdue_hours = Decimal(ceil(overdue_hours))
        return (policy.late_fee_per_hour * overdue_hours).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

    @property
    def overdue_hours(self):
        if not self.end_time:
            return 0
        actual = timezone.now() if self.status == 'Active' else self.return_date
        if not actual:
            return 0
        overdue = actual - self.end_time
        overdue_seconds = int(overdue.total_seconds())
        if overdue_seconds <= 0:
            return 0
        return (overdue_seconds + 3599) // 3600

    @property
    def days_overdue(self):
        if not self.end_time:
            return 0
        actual = timezone.now() if self.status == 'Active' else self.return_date
        if not actual:
            return 0
        overdue = actual - self.end_time
        seconds = int(overdue.total_seconds())
        if seconds <= 0:
            return 0
        return (seconds + 86399) // 86400

    @property
    def late_fee(self):
        if self.status == 'Returned' and self.return_date:
            try:
                return self.late_fee_transaction.amount
            except LateFeeTransaction.DoesNotExist:
                pass
            return self._calculate_late_fee(self.return_date)
        if self.status == 'Active' and self.end_time:
            return self._calculate_late_fee(timezone.now())
        return Decimal('0.00')

    @property
    def price(self):
        return self.rental_subtotal

    @property
    def rental_subtotal(self):
        if not self.suit_id:
            return Decimal("0.00")
        price_per_day = (
            self.suit_price_per_day
            if self.suit_price_per_day is not None
            else self.suit.price_per_day
        )
        return price_per_day * self.days_requested * self.quantity_requested

    @property
    def booked_suit_price(self):
        if not self.suit_id:
            return Decimal("0.00")
        return (
            self.suit_price_per_day
            if self.suit_price_per_day is not None
            else self.suit.price_per_day
        )

    @property
    def accessory_items(self):
        """Parse accessory details from notes so return audits can show returned accessories."""
        if self.accessory_details:
            return [
                f"{item} ({(details or {}).get('size') or 'Standard'} / {(details or {}).get('color') or 'Standard'})"
                for item, details in self.accessory_details.items()
            ]
        if not self.notes:
            return []

        for line in self.notes.splitlines():
            if line.strip().startswith("Accessories:"):
                accessory_text = line.split("Accessories:", 1)[1].strip()
                if not accessory_text:
                    return []
                return [item.strip() for item in accessory_text.split(",") if item.strip()]
        return []

    @property
    def returnable_items(self):
        items = []
        if self.suit_id:
            items.append(f"{self.suit.suit_name} (Qty: {self.quantity_requested or 1})")
        items.extend(self.accessory_items)
        return items

    @property
    def rental_duration_hours(self):
        return self.DEFAULT_RENTAL_DURATION_MINUTES / 60

    def _default_due_date(self, base_time=None):
        current_time = base_time or timezone.now()
        requested_days = self.days_requested or self.rent_days or 1
        return current_time + timedelta(days=requested_days)

    @staticmethod
    def _reserved_quantity(
        suit_id,
        exclude_request_id=None,
        requested_start_date=None,
        requested_end_date=None,
    ):
        reservations = SuitRequest.objects.filter(
            suit_id=suit_id,
            status="Reserved",
        ).filter(Q(due_date__isnull=True) | Q(due_date__gt=timezone.now()))
        if requested_start_date and requested_end_date:
            reservations = reservations.filter(
                Q(
                    requested_start_date__lte=requested_end_date,
                    requested_end_date__gte=requested_start_date,
                )
                | Q(requested_start_date__isnull=True)
                | Q(requested_end_date__isnull=True)
            )
        if exclude_request_id is not None:
            reservations = reservations.exclude(pk=exclude_request_id)
        return reservations.aggregate(total=Sum("quantity_requested"))["total"] or 0

    def reserve_inventory(self, expires_at, performed_by=None):
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())

        with transaction.atomic():
            rental = SuitRequest.objects.select_for_update().get(pk=self.pk)
            if rental.status == "Reserved" and (
                rental.due_date is None or rental.due_date > timezone.now()
            ):
                self.refresh_from_db()
                return False
            if rental.status not in {"Pending", "Approved", "Reserved"}:
                raise ValidationError("Only pending or approved requests can reserve inventory.")

            if not rental.suit_id:
                self.refresh_from_db()
                return True

            suit = Suit.objects.select_for_update().get(pk=rental.suit_id)
            quantity = rental.quantity_requested or 0
            requested_start_date = rental.requested_start_date or timezone.localdate()
            requested_end_date = rental.requested_end_date or (
                requested_start_date + timedelta(days=(rental.days_requested or 1))
            )
            other_reservations = self._reserved_quantity(
                suit.pk,
                rental.pk,
                requested_start_date,
                requested_end_date,
            )
            if quantity < 1:
                raise ValidationError("Reservation quantity must be greater than zero.")
            if suit.is_retired or suit.quantity - other_reservations < quantity:
                available = max(suit.quantity - other_reservations, 0)
                raise ValidationError(
                    f"Only {available} unreserved units of {suit.suit_name} are currently available."
                )

            old_workflow_status = rental.workflow_status
            rental.status = "Reserved"
            rental.due_date = expires_at
            rental.workflow_status = "waiting"
            rental.waiting_deadline = expires_at
            rental.save(update_fields=[
                "status", "due_date", "workflow_status", "waiting_deadline",
            ])
            if old_workflow_status != "waiting":
                StatusHistory.objects.create(
                    booking=rental,
                    old_status=old_workflow_status,
                    new_status="waiting",
                    changed_by=performed_by,
                    note="Reservation hold started.",
                )
            record_inventory_movement(
                suit,
                "reserved",
                performed_by=performed_by,
                rental_request=rental,
                notes=f"Reserved {quantity} unit(s) until {expires_at.isoformat()}.",
            )

            suit.status = (
                "Available"
                if suit.quantity - other_reservations - quantity > 0
                else "Reserved"
            )
            suit.save(update_fields=["status"])
            self.refresh_from_db()
            return True

    def start_rental(self, start_time=None, performed_by=None):
        if start_time is None:
            start_time = timezone.now()
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time, timezone.get_current_timezone())

        with transaction.atomic():
            rental = SuitRequest.objects.select_for_update().get(pk=self.pk)
            if rental.status == "Active":
                self.refresh_from_db()
                return False
            if rental.status not in {"Pending", "Approved", "Reserved"}:
                raise ValidationError("Only pending, approved, or reserved rentals can be issued.")

            suit = Suit.objects.select_for_update().get(pk=rental.suit_id) if rental.suit_id else None
            quantity = rental.quantity_requested or 0
            other_reservations = 0
            if suit:
                requested_start_date = rental.requested_start_date or timezone.localtime(start_time).date()
                requested_end_date = rental.requested_end_date or (
                    requested_start_date + timedelta(days=(rental.days_requested or 1))
                )
                other_reservations = self._reserved_quantity(
                    suit.pk,
                    rental.pk,
                    requested_start_date,
                    requested_end_date,
                )
                if quantity < 1:
                    raise ValidationError("Rental quantity must be greater than zero.")
                if suit.is_retired or suit.quantity - other_reservations < quantity:
                    available = max(suit.quantity - other_reservations, 0)
                    raise ValidationError(
                        f"Only {available} unreserved units of {suit.suit_name} are currently available."
                    )

            rental.rent_days = rental.days_requested or rental.rent_days or 1
            if not rental.approved_at:
                rental.approved_at = timezone.now()
            rental.rental_start_time = start_time
            rental.due_date = rental._default_due_date(start_time)
            rental.end_time = rental.due_date
            rental.return_date = None
            rental.status = "Active"
            if not suit and not rental.accessory_details:
                raise ValidationError("An accessory-only rental must include at least one accessory.")
            if not rental.total_amount and suit and not rental.discount_amount:
                rental.total_amount = rental.booked_suit_price * rental.days_requested * quantity
            rental.save(update_fields=[
                "rent_days", "approved_at", "rental_start_time", "due_date",
                "end_time", "return_date", "status", "total_amount",
            ])

            if suit:
                suit.quantity -= quantity
                remaining_unreserved = suit.quantity - other_reservations
                suit.status = (
                    "Available" if remaining_unreserved > 0
                    else "Reserved" if other_reservations > 0
                    else "Rented"
                )
                suit.save(update_fields=["quantity", "status"])
                record_inventory_movement(
                    suit,
                    "issued",
                    quantity_delta=-quantity,
                    performed_by=performed_by,
                    rental_request=rental,
                    notes="Rental issued to customer.",
                )
                suit.refresh_rental_stats()
                suit.refresh_rental_end_time()

            self.refresh_from_db()
            return True

    def return_suit(self, performed_by=None, return_condition="good"):
        if return_condition not in dict(self.RETURN_CONDITION_CHOICES):
            raise ValidationError("Select a valid returned-item condition.")

        with transaction.atomic():
            rental = SuitRequest.objects.select_for_update().get(pk=self.pk)
            if rental.status != "Active":
                self.refresh_from_db()
                return False

            returned_at = timezone.now()
            rental.return_date = returned_at
            late_fee_amount = rental._calculate_late_fee(returned_at)
            rental.total_amount += late_fee_amount
            rental.status = "Returned"
            rental.return_condition = return_condition
            rental.returned_by = performed_by
            rental.save(update_fields=[
                "return_date", "total_amount", "status",
                "return_condition", "returned_by",
            ])
            record_late_fee_transaction(
                rental,
                late_fee_amount,
                returned_at,
                performed_by=performed_by,
            )

            suit = Suit.objects.select_for_update().get(pk=rental.suit_id) if rental.suit_id else None
            if not suit:
                self.refresh_from_db()
                return True

            if suit.should_queue_for_dry_cleaning or rental.return_condition == "damaged":
                suit.status = "Dry Cleaning"
                movement_type = "returned_for_cleaning"
                quantity_delta = 0
            else:
                suit.quantity += rental.quantity_requested or 1
                suit.status = "Available"
                movement_type = "returned"
                quantity_delta = rental.quantity_requested or 1
            suit.save(update_fields=["status", "quantity"])
            record_inventory_movement(
                suit,
                movement_type,
                quantity_delta=quantity_delta,
                performed_by=performed_by,
                rental_request=rental,
                notes=(
                    "Damaged item held out of available stock pending cleaning or repair."
                    if rental.return_condition == "damaged"
                    else "Customer return processed."
                ),
            )
            suit.refresh_rental_stats()
            suit.refresh_rental_end_time()

            self.refresh_from_db()
            return True

    @property
    def amount_paid(self):
        total = self.payments.aggregate(total=Sum('amount_paid'))['total']
        return total if total is not None else Decimal('0.00')

    @property
    def guarantee_collected(self):
        total = self.payments.aggregate(total=Sum("guarantee_amount"))["total"]
        return total if total is not None else Decimal("0.00")

    @property
    def guarantee_held(self):
        return max(self.guarantee_collected - self.guarantee_refunded, Decimal("0.00"))

    @property
    def balance_due(self):
        due = self.total_amount - self.amount_paid
        return max(due, Decimal('0.00'))


class StatusHistory(models.Model):
    booking = models.ForeignKey(
        SuitRequest,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    old_status = models.CharField(max_length=12, blank=True)
    new_status = models.CharField(max_length=12, choices=SuitRequest.WORKFLOW_STATUS_CHOICES)
    changed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="booking_status_changes",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["changed_at", "pk"]

    def __str__(self):
        return f"Booking #{self.booking_id}: {self.old_status} → {self.new_status}"


class RentalPolicy(models.Model):
    PARTIAL_HOUR_CHOICES = [
        ("round_up", "Round up each started hour"),
        ("prorate", "Prorate by actual overdue time"),
    ]

    late_fee_per_hour = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.50"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    partial_hour_policy = models.CharField(
        max_length=20,
        choices=PARTIAL_HOUR_CHOICES,
        default="round_up",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        policy, _ = cls.objects.get_or_create(pk=1)
        return policy

    def __str__(self):
        return "Rental business policy"


class LateFeeTransaction(models.Model):
    rental = models.OneToOneField(
        SuitRequest,
        on_delete=models.PROTECT,
        related_name="late_fee_transaction",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    overdue_seconds = models.PositiveBigIntegerField()
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2)
    partial_hour_policy = models.CharField(
        max_length=20,
        choices=RentalPolicy.PARTIAL_HOUR_CHOICES,
    )
    assessed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assessed_late_fees",
    )
    assessed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-assessed_at", "-pk"]

    def __str__(self):
        return f"Late fee for rental #{self.rental_id}: ${self.amount}"


def record_late_fee_transaction(rental, amount, actual_return, performed_by=None):
    if amount <= 0:
        return None

    due_time = rental.due_date or rental.end_time
    overdue_seconds = max(int((actual_return - due_time).total_seconds()), 0) if due_time else 0
    policy = RentalPolicy.get_solo()
    transaction_record, _ = LateFeeTransaction.objects.get_or_create(
        rental=rental,
        defaults={
            "amount": amount,
            "overdue_seconds": overdue_seconds,
            "hourly_rate": policy.late_fee_per_hour,
            "partial_hour_policy": policy.partial_hour_policy,
            "assessed_by": performed_by,
            "assessed_at": actual_return,
        },
    )
    return transaction_record


class InventoryMovement(models.Model):
    MOVEMENT_TYPES = [
        ("opening_balance", "Opening Balance"),
        ("adjustment", "Stock Adjustment"),
        ("reserved", "Reserved"),
        ("reservation_released", "Reservation Released"),
        ("issued", "Issued to Customer"),
        ("returned", "Returned to Available Stock"),
        ("returned_for_cleaning", "Returned for Cleaning"),
        ("cleaning_completed", "Cleaning Completed"),
    ]

    suit = models.ForeignKey(
        Suit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_movements",
    )
    suit_name = models.CharField(max_length=100)
    rental_request = models.ForeignKey(
        SuitRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_movements",
    )
    movement_type = models.CharField(max_length=30, choices=MOVEMENT_TYPES)
    quantity_delta = models.IntegerField(default=0)
    quantity_after = models.PositiveIntegerField()
    performed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_movements",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["suit", "-created_at"]),
            models.Index(fields=["movement_type", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.suit_name}: {self.get_movement_type_display()} ({self.quantity_delta:+d})"


def record_inventory_movement(
    suit,
    movement_type,
    quantity_delta=0,
    performed_by=None,
    rental_request=None,
    notes="",
):
    return InventoryMovement.objects.create(
        suit=suit,
        suit_name=suit.suit_name,
        rental_request=rental_request,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        quantity_after=suit.quantity,
        performed_by=performed_by,
        notes=notes,
    )


class Staff(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('terminated', 'Terminated'),
    ]

    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30, blank=True, null=True)
    position = models.CharField(max_length=100, blank=True, null=True)
    monthly_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    hire_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    class Meta:
        ordering = ['-hire_date', 'name']

    def __str__(self):
        return f"{self.name} ({self.position or 'Staff'})"

    def total_paid_for_month(self, month, year):
        res = self.payrolls.filter(payment_type__iexact='Salary', month=month, year=year).aggregate(total=Sum('amount'))
        return res['total'] or Decimal('0.00')

    def total_advances(self):
        res = self.payrolls.filter(payment_type__iexact='Advance').aggregate(total=Sum('amount'))
        return res['total'] or Decimal('0.00')

    def remaining_salary_for_month(self, month, year):
        paid = self.total_paid_for_month(month, year)
        return Decimal(self.monthly_salary) - Decimal(paid)


class Payroll(models.Model):
    PAYMENT_TYPE_CHOICES = [
        ('Salary', 'Salary'),
        ('Advance', 'Advance'),
        ('Bonus', 'Bonus'),
        ('Deduction', 'Deduction'),
    ]

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='payrolls')
    payment_date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES, default='Salary')
    month = models.IntegerField(null=True, blank=True)
    year = models.IntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='created_payrolls')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f"{self.staff.name} - {self.payment_type} - {self.amount} on {self.payment_date}"


class Payment(models.Model):
    STATUS_CHOICES = [
        ('paid', 'Paid'),
        ('partial', 'Partial'),
        ('refunded', 'Refunded'),
        ('partial_refund', 'Partial Refund'),
    ]

    rental = models.ForeignKey(SuitRequest, on_delete=models.CASCADE, related_name='payments')
    cashier = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    guarantee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    idempotency_key = models.CharField(max_length=36, unique=True, null=True, blank=True)
    payment_method = models.CharField(max_length=50, blank=True, null=True)
    payment_reference = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='paid')
    payment_date = models.DateTimeField(default=timezone.now)
    receipt_number = models.CharField(max_length=50, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    refund_method = models.CharField(max_length=50, blank=True, null=True)
    refund_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.receipt_number or f"Payment #{self.id}"

    @property
    def customer_name(self):
        return self.rental.name

    @property
    def phone(self):
        return self.rental.phone

    @property
    def suit_name(self):
        return self.rental.suit.suit_name if self.rental.suit_id else "Accessories only"

    @property
    def method(self):
        return self.payment_method

    @property
    def balance_due(self):
        return self.rental.balance_due

    @property
    def total_collected(self):
        return self.amount_paid + self.guarantee_amount


class Expense(models.Model):
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    expense_date = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.description} - ${self.amount}"


class FavoriteSuit(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    suit = models.ForeignKey(Suit, on_delete=models.CASCADE)
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('user', 'suit')
        ordering = ['-added_at']

    def __str__(self):
        return f"{self.user.username} favorites {self.suit.suit_name}"


@receiver(post_save, sender=SuitRequest)
def create_initial_status_history(sender, instance, created, **kwargs):
    if not created:
        return

    legacy_to_workflow = {
        "Pending": "request",
        "Approved": "pending",
        "Reserved": "waiting",
        "Active": "rented",
        "Returned": "returned",
        "Rejected": "cancelled",
        # Legacy Expired means an overdue item that still needs to be returned.
        "Expired": "rented",
    }
    workflow_status = instance.workflow_status
    if workflow_status == "request" and instance.status in legacy_to_workflow:
        workflow_status = legacy_to_workflow[instance.status]
        SuitRequest.objects.filter(pk=instance.pk).update(workflow_status=workflow_status)
        instance.workflow_status = workflow_status

    StatusHistory.objects.create(
        booking=instance,
        old_status="",
        new_status=workflow_status,
        changed_by=None,
        changed_at=instance.request_date,
        note="Booking la sameeyay.",
    )


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)

    def __str__(self):
        return f"Notification for {self.user.username}: {self.message[:50]}"


# Extend user with a lightweight profile for uploads
class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    profile_photo = models.ImageField(
        upload_to='profiles/',
        storage=private_media_storage,
        null=True,
        blank=True,
    )
    id_document = models.FileField(
        upload_to='ids/',
        storage=private_media_storage,
        null=True,
        blank=True,
    )
    customer_phone = models.CharField(max_length=20, blank=True)
    national_id = models.CharField(max_length=50, blank=True)
    present_address = models.CharField(max_length=255, blank=True)
    parents_address = models.CharField(max_length=255, blank=True)
    is_cashier = models.BooleanField(default=False)
    is_reception = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile for {self.user.username}"


from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)
    else:
        # Ensure profile exists for existing users
        Profile.objects.get_or_create(user=instance)
