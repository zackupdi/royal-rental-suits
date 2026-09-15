from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import timedelta
from math import ceil
import datetime
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal


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
        return "https://images.unsplash.com/photo-1594932224011-04504106503c?q=80&w=800"

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
        return self.quantity

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
        if self.is_retired:
            return False
        if self.status == "Available":
            return self.quantity > 0
        if self.status == "Dry Cleaning":
            return self.quantity > 1
        return False

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
        ('pending', 'Pending Admin Review'),
        ('approved', 'Approved by Admin'),
        ('rejected', 'Rejected by Admin'),
    ]
    
    suit = models.ForeignKey('Suit', on_delete=models.CASCADE, related_name='dry_cleanings')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    date = models.DateField(default=timezone.now)
    notes = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.suit} - ${self.amount} ({'Paid' if self.paid else 'Unpaid'})"


class SuitRequest(models.Model):
    DEFAULT_RENTAL_DURATION_MINUTES = 24 * 60

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    suit = models.ForeignKey(Suit, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    customer_email = models.EmailField(blank=True, null=True)  # Customer's email for notifications
    national_id = models.CharField(max_length=50, blank=True, null=True)
    # Additional customer name/address fields
    first_name = models.CharField(max_length=50, blank=True, null=True)
    second_name = models.CharField(max_length=50, blank=True, null=True)
    present_address = models.CharField(max_length=255, blank=True, null=True)
    parents_address = models.CharField(max_length=255, blank=True, null=True)

    customer_image = models.ImageField(upload_to="customers/", blank=True, null=True)

    request_date = models.DateTimeField(default=timezone.now)

    rental_start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    return_date = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    days_requested = models.IntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(7)])  # Allow up to 7 days
    rent_days = models.PositiveIntegerField(default=1)
    approved_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateTimeField(null=True, blank=True)
    quantity_requested = models.IntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(5)])  # Allow up to 5 suits
    notes = models.TextField(blank=True, null=True)

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

        overdue_minutes = ceil(overdue_seconds / 60)
        if overdue_minutes <= 0:
            return Decimal('0.00')

        return Decimal(overdue_minutes).quantize(Decimal('0.01'))

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
            return self._calculate_late_fee(self.return_date)
        if self.status == 'Active' and self.end_time:
            return self._calculate_late_fee(timezone.now())
        return Decimal('0.00')

    @property
    def price(self):
        return self.rental_subtotal

    @property
    def rental_subtotal(self):
        return self.suit.price_per_day * self.days_requested * self.quantity_requested

    @property
    def accessory_items(self):
        """Parse accessory details from notes so return audits can show returned accessories."""
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
        if self.suit:
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

    def start_rental(self, start_time=None):
        if start_time is None:
            start_time = timezone.now()

        if self.rent_days < 1:
            self.rent_days = self.days_requested or 1
        if not self.approved_at:
            self.approved_at = start_time
        if not self.due_date:
            self.due_date = self._default_due_date(self.approved_at)

        self.rental_start_time = start_time
        self.end_time = self.due_date or self.rental_start_time + timedelta(minutes=self.DEFAULT_RENTAL_DURATION_MINUTES)
        self.return_date = self.end_time
        self.status = "Active"
        if not self.total_amount:
            self.total_amount = self.suit.price_per_day * self.days_requested * self.quantity_requested
        self.save()

        suit = self.suit
        suit.quantity -= self.quantity_requested
        suit.status = "Rented"
        suit.save()
        suit.refresh_rental_stats()
        suit.refresh_rental_end_time()

    def return_suit(self):
        self.return_date = timezone.now()
        late_fee = self._calculate_late_fee(self.return_date)
        self.total_amount += late_fee
        self.status = "Returned"
        self.save()

        suit = self.suit
        if suit:
            if suit.should_queue_for_dry_cleaning:
                suit.status = 'Dry Cleaning'
            else:
                suit.quantity += self.quantity_requested or 1
                suit.status = 'Available'
            suit.save(update_fields=['status', 'quantity'])
            suit.refresh_rental_stats()
            try:
                suit.refresh_rental_end_time()
            except Exception:
                pass

    @property
    def amount_paid(self):
        total = self.payments.aggregate(total=Sum('amount_paid'))['total']
        return total if total is not None else Decimal('0.00')

    @property
    def balance_due(self):
        due = self.total_amount - self.amount_paid
        return max(due, Decimal('0.00'))


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
        return self.rental.suit.suit_name

    @property
    def method(self):
        return self.payment_method

    @property
    def balance_due(self):
        return self.rental.balance_due


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
    profile_photo = models.ImageField(upload_to='profiles/', null=True, blank=True)
    id_document = models.FileField(upload_to='ids/', null=True, blank=True)
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
