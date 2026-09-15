import base64
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import ContactMessage, DryCleaning, Expense, Payment, Profile, Suit, SuitRequest


class PublicViewsModuleTests(TestCase):
    def test_public_views_module_exports_core_views(self):
        from . import views_public

        self.assertTrue(callable(views_public.home))
        self.assertTrue(callable(views_public.contact_page))
        self.assertTrue(callable(views_public.customer_login))
        self.assertTrue(callable(views_public.login_view))

    def test_staff_login_page_mentions_reception_access(self):
        response = self.client.get(reverse("admin_login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reception")
        self.assertNotContains(response, "Cashier")


class ReceptionDashboardFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reception", password="testpass123")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.profile = self.user.profile
        self.profile.is_reception = True
        self.profile.save(update_fields=["is_reception"])

        self.suit = Suit.objects.create(
            suit_name="Test Suit",
            price_per_day=Decimal("25.00"),
            quantity=3,
            status="Available",
        )
        SuitRequest.objects.create(
            suit=self.suit,
            name="Test Customer",
            phone="252611111111",
            total_amount=Decimal("75.00"),
            status="Active",
            quantity_requested=1,
        )
        Expense.objects.create(description="Dry cleaning", amount=Decimal("12.50"), created_by=self.user)

    def test_reception_page_uses_reception_booking_template_with_live_context(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reception"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception.html")
        self.assertContains(response, "Available Suits Showroom")
        self.assertContains(response, "Dry Cleaning")
        self.assertContains(response, "3")

    def test_returning_suit_marks_it_for_cleaning_without_restoring_availability(self):
        suit = Suit.objects.create(
            suit_name="Cleaning Test Suit",
            price_per_day=Decimal("30.00"),
            quantity=2,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Cleaning Customer",
            phone="252611111111",
            total_amount=Decimal("60.00"),
            status="Active",
            quantity_requested=1,
            days_requested=2,
        )

        req.start_rental(start_time=timezone.now() - timezone.timedelta(days=1))
        req.return_suit()

        suit.refresh_from_db()
        req.refresh_from_db()

        self.assertEqual(req.status, "Returned")
        self.assertEqual(suit.status, "Dry Cleaning")
        self.assertEqual(suit.quantity, 1)

    def test_dry_cleaning_suit_is_not_available_when_no_other_units_exist(self):
        suit = Suit.objects.create(
            suit_name="Cleaning Availability Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Dry Cleaning",
        )

        self.assertFalse(suit.is_available_for_booking)

    def test_returning_accessory_item_marks_it_available_instead_of_dry_cleaning(self):
        suit = Suit.objects.create(
            suit_name="Formal Shoes",
            price_per_day=Decimal("10.00"),
            quantity=1,
            status="Available",
            item_type="shoes",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Accessory Customer",
            phone="252611111111",
            total_amount=Decimal("10.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=1,
        )

        req.start_rental(start_time=timezone.now())
        req.return_suit()

        suit.refresh_from_db()
        self.assertEqual(suit.status, "Available")
        self.assertEqual(suit.quantity, 1)

    def test_suit_api_returns_item_category_for_edit_form(self):
        suit = Suit.objects.create(
            suit_name="Edit Form Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
            item_type="shoes",
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("suit_api_detail", args=[suit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["category"], "shoes")

    def test_suit_profile_page_uses_live_rental_metrics_and_history(self):
        self.client.login(username="reception", password="testpass123")
        suit = Suit.objects.create(
            suit_name="Profile Metrics Suit",
            price_per_day=Decimal("30.00"),
            quantity=2,
            status="Available",
        )
        SuitRequest.objects.create(
            suit=suit,
            name="First Customer",
            phone="252611000001",
            total_amount=Decimal("45.00"),
            status="Returned",
            quantity_requested=1,
            request_date=timezone.now() - timezone.timedelta(days=2),
        )
        SuitRequest.objects.create(
            suit=suit,
            name="Second Customer",
            phone="252611000002",
            total_amount=Decimal("30.00"),
            status="Active",
            quantity_requested=1,
            request_date=timezone.now() - timezone.timedelta(days=1),
        )

        response = self.client.get(reverse("suit_profile", args=[suit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 times")
        self.assertContains(response, "$75.00")
        self.assertContains(response, "Usage History")
        self.assertContains(response, "First Customer")
        self.assertContains(response, "Second Customer")

    def test_rental_uses_one_day_timeout_by_default(self):
        suit = Suit.objects.create(
            suit_name="Duration Test Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Duration Customer",
            phone="252611111111",
            total_amount=Decimal("40.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=2,
        )

        start_time = timezone.now()
        req.start_rental(start_time=start_time)

        self.assertEqual(req.end_time - start_time, timezone.timedelta(days=2))

    def test_active_rental_charges_late_fee_per_minute(self):
        suit = Suit.objects.create(
            suit_name="Late Fee Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Late Fee Customer",
            phone="252611111112",
            total_amount=Decimal("20.00"),
            status="Active",
            quantity_requested=1,
            days_requested=1,
        )

        req.rental_start_time = timezone.now() - timezone.timedelta(minutes=10)
        req.due_date = timezone.now() - timezone.timedelta(minutes=5)
        req.end_time = req.due_date
        req.save(update_fields=["rental_start_time", "due_date", "end_time"])

        self.assertEqual(req.late_fee, Decimal("5.00"))

    def test_partial_minute_overdue_is_rounded_up_to_next_minute(self):
        suit = Suit.objects.create(
            suit_name="Rounded Late Fee Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Rounded Fee Customer",
            phone="252611111113",
            total_amount=Decimal("20.00"),
            status="Active",
            quantity_requested=1,
            days_requested=1,
        )

        req.rental_start_time = timezone.now() - timezone.timedelta(minutes=2)
        req.due_date = timezone.now() - timezone.timedelta(minutes=1, seconds=10)
        req.end_time = req.due_date
        req.save(update_fields=["rental_start_time", "due_date", "end_time"])

        self.assertEqual(req.late_fee, Decimal("2.00"))

    def test_dry_cleaning_ledger_shows_recent_queue_and_expenses(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("dry_cleaning_ledger"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cleaning Expense Ledger")
        self.assertContains(response, "Dry cleaning")

    def test_dry_cleaning_ledger_uses_dry_cleaning_records_from_database(self):
        suit = Suit.objects.create(
            suit_name="Ledger Suit",
            price_per_day=Decimal("18.00"),
            quantity=1,
            status="Dry Cleaning",
        )
        DryCleaning.objects.create(
            suit=suit,
            amount=Decimal("12.50"),
            status="approved",
            paid=True,
            notes="Approved cleaning",
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("dry_cleaning_ledger"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ledger Suit")
        self.assertContains(response, "Approved")
        self.assertContains(response, "12.50")

    def test_laundry_queue_supports_tabs_and_pagination(self):
        self.client.login(username="reception", password="testpass123")

        for idx in range(12):
            suit = Suit.objects.create(
                suit_name=f"Laundry Suit {idx}",
                price_per_day=Decimal("20.00"),
                quantity=1,
                status="Available",
            )
            SuitRequest.objects.create(
                suit=suit,
                name=f"Customer {idx}",
                phone="252611111111",
                total_amount=Decimal("40.00"),
                status="Returned",
                quantity_requested=1,
            )

        response = self.client.get(reverse("laundry_backlog"), {"tab": "pending", "page": 1})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending Review")
        self.assertContains(response, "page=2")
        self.assertContains(response, "Submit Payment")

    def test_mark_ready_makes_suit_available_and_removes_it_from_queue(self):
        suit = Suit.objects.create(
            suit_name="Ready Suit",
            price_per_day=Decimal("22.00"),
            quantity=1,
            status="Dry Cleaning",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Ready Customer",
            phone="252611111111",
            total_amount=Decimal("44.00"),
            status="Returned",
            quantity_requested=1,
        )
        dry_clean = DryCleaning.objects.create(
            suit=suit,
            amount=Decimal("8.00"),
            status="approved",
            paid=False,
            notes="Approved cleaning",
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("laundry_backlog"),
            {
                "action": "mark_ready",
                "request_id": req.id,
                "suit_id": suit.id,
                "cleaning_cost": "8.00",
                "laundry_notes": "Ready",
            },
            follow=True,
        )

        suit.refresh_from_db()
        req.refresh_from_db()
        dry_clean.refresh_from_db()

        self.assertEqual(suit.status, "Available")
        self.assertEqual(suit.quantity, 2)
        self.assertTrue(dry_clean.paid)
        self.assertTrue(Expense.objects.filter(description__icontains="Dry cleaning for Ready Suit").exists())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dry Cleaning Terminal Clear")
        self.assertNotContains(response, 'data-suit-name="Ready Suit"')

    def test_reception_requests_page_can_mark_ready_for_approved_dry_cleaning(self):
        suit = Suit.objects.create(
            suit_name="Reception Ready Suit",
            price_per_day=Decimal("22.00"),
            quantity=1,
            status="Dry Cleaning",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Reception Customer",
            phone="252611111111",
            total_amount=Decimal("44.00"),
            status="Returned",
            quantity_requested=1,
        )
        dry_clean = DryCleaning.objects.create(
            suit=suit,
            amount=Decimal("8.00"),
            status="approved",
            paid=True,
            notes="Approved cleaning",
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("reception_requests"),
            {
                "action": "mark_ready",
                "request_id": req.id,
                "suit_id": suit.id,
                "cleaning_cost": "8.00",
                "laundry_notes": "Ready from reception",
            },
            follow=True,
        )

        suit.refresh_from_db()
        req.refresh_from_db()
        dry_clean.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(suit.status, "Available")
        self.assertEqual(suit.quantity, 2)
        self.assertEqual(req.status, "Returned")
        self.assertTrue(dry_clean.paid)
        self.assertContains(response, "ready")

    def test_reception_reports_page_renders_without_template_errors(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception_reports.html")
        self.assertContains(response, "Business Reports")

    def test_reception_reports_page_shows_daily_reception_summary(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Daily Reception Report")
        self.assertContains(response, "Today")

    def test_reception_reports_page_shows_recent_activity_dashboard(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent Activity")
        self.assertContains(response, "Live database")

    def test_add_expense_uses_selected_reception_user_from_dropdown(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("add_expense"),
            {
                "amount": "20.00",
                "category": "Supplies",
                "date": timezone.localdate().strftime("%Y-%m-%d"),
                "description": "Office supplies",
                "assigned_user": str(self.user.id),
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("reception_reports"))
        expense = Expense.objects.latest("id")
        self.assertEqual(expense.created_by, self.user)
        self.assertEqual(expense.notes, "Supplies")

    def test_reception_desk_uses_reception_template_with_live_booking_data(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reception"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception.html")
        self.assertContains(response, "Reception Desk")
        self.assertContains(response, "Active & Pending Bookings Log")
        self.assertContains(response, "Test Customer")

    def test_admin_booking_route_uses_admin_template(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("admin_booking"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/admin_booking.html")
        self.assertContains(response, "Create New Rental Form")

    def test_create_rental_without_suit_id_redirects_with_error(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("create_rental"),
            {
                "suit_id": "",
                "quantity": "1",
                "days": "2",
                "start_date": "2026-07-05",
                "end_date": "2026-07-07",
                "first_name": "Ahmed",
                "phone": "252611223344",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("reception_reception"))
        self.assertContains(response, "Please select a suit")

    def test_approve_request_sets_due_date_to_requested_days(self):
        suit = Suit.objects.create(
            suit_name="Approval Due Date Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Approval Customer",
            phone="252611223344",
            total_amount=Decimal("40.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=2,
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
                "start_date": "",
            },
            follow=True,
        )

        req.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(req.rent_days, 2)
        self.assertIsNotNone(req.approved_at)
        self.assertIsNotNone(req.due_date)
        self.assertEqual(req.due_date - req.approved_at, timezone.timedelta(days=2))

    def test_add_suit_page_uses_reception_add_item_template(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("add_suit_new"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception_add_item.html")
        self.assertContains(response, "Add New Item")

    def test_reception_add_item_url_name_resolves(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_add_item"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception_add_item.html")

    def test_reception_add_item_post_creates_inventory_item(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("add_suit_new"),
            {
                "category": "suits",
                "suit_name": "Inventory Suit Test",
                "collection": "Standard",
                "description": "Test suit created by reception",
                "size": "M",
                "color": "Black",
                "quantity": "2",
                "price_per_day": "22.50",
                "condition_grade": "new",
                "damage_notes": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Suit.objects.filter(suit_name="Inventory Suit Test").exists())
        new_suit = Suit.objects.get(suit_name="Inventory Suit Test")
        self.assertEqual(new_suit.collection, "Standard")
        self.assertEqual(new_suit.size, "M")
        self.assertEqual(new_suit.color, "Black")
        self.assertEqual(new_suit.quantity, 2)
        self.assertEqual(new_suit.price_per_day, Decimal("22.50"))
        self.assertEqual(new_suit.status, "Available")

    def test_reception_add_item_preserves_item_type_for_accessories(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("add_suit_new"),
            {
                "category": "shoes",
                "suit_name": "Formal Shoes",
                "collection": "Standard",
                "description": "Leather dress shoes",
                "size": "43",
                "color": "Black",
                "quantity": "1",
                "price_per_day": "10.00",
                "condition_grade": "new",
                "damage_notes": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        new_suit = Suit.objects.get(suit_name="Formal Shoes")
        self.assertEqual(new_suit.item_type, "shoes")
        self.assertEqual(new_suit.size, "43")
        self.assertEqual(new_suit.price_per_day, Decimal("10.00"))

    def test_reception_requests_page_renders_without_template_errors(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_requests"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception_requests.html")
        self.assertContains(response, "Active Rental & Requests Registry")

    def test_reception_payments_page_renders_daily_reports_with_live_context(self):
        self.client.login(username="reception", password="testpass123")

        suit = Suit.objects.create(
            suit_name="Payments Suit",
            price_per_day=Decimal("40.00"),
            quantity=2,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Test Customer",
            phone="252611223344",
            total_amount=Decimal("80.00"),
            status="Active",
            quantity_requested=1,
        )
        Payment.objects.create(
            rental=rental,
            cashier=self.user,
            amount_paid=Decimal("30.00"),
            payment_method="cash",
            status="paid",
            payment_date=timezone.now(),
            receipt_number="R-1001",
        )
        Expense.objects.create(description="Cleaning supply", amount=Decimal("5.00"), created_by=self.user)

        response = self.client.get(reverse("reception_payments"), {"search": "Test Customer"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception_payments.html")
        self.assertContains(response, "Completed Work")
        self.assertContains(response, "Recorded Expenses")
        self.assertContains(response, "Daily Profit")
        self.assertNotContains(response, "Total Payments")
        self.assertContains(response, "Daily Financial Ledger")
        self.assertContains(response, "Test Customer")
        self.assertContains(response, "Cleaning supply")
        self.assertContains(response, "Record Payment")
        self.assertContains(response, "Receipt Preview")


class AdminUserManagementTests(TestCase):
    def test_manage_users_page_renders_admin_dashboard(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        self.client.login(username="admin", password="testpass123")

        response = self.client.get(reverse("manage_users"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/manage_users.html")
        self.assertContains(response, "User Management")
        self.assertContains(response, "Add Cashier")
        self.assertContains(response, "Add Reception")

    def test_protects_primary_admin_from_self_lockout_and_deletion(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.is_superuser = True
        admin.save(update_fields=["is_staff", "is_superuser"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        self.client.login(username="admin", password="testpass123")

        deactivate_response = self.client.get(reverse("toggle_user_active", args=[admin.id]), follow=True)
        self.assertRedirects(deactivate_response, reverse("manage_users"))
        self.assertContains(deactivate_response, "cannot be")
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)

        delete_response = self.client.get(reverse("delete_user", args=[admin.id]), follow=True)
        self.assertRedirects(delete_response, reverse("manage_users"))
        self.assertContains(delete_response, "cannot delete")
        self.assertTrue(User.objects.filter(id=admin.id).exists())

    def test_customer_detail_view_shows_user_rental_history(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        customer = User.objects.create_user(username="customer", password="testpass123")
        suit = Suit.objects.create(suit_name="Test Suit", price_per_day=Decimal("20.00"), quantity=1, status="Available")
        rental = SuitRequest.objects.create(
            user=customer,
            suit=suit,
            name="Customer User",
            phone="252611223344",
            total_amount=Decimal("40.00"),
            status="Returned",
            quantity_requested=1,
        )

        self.client.login(username="admin", password="testpass123")

        response = self.client.get(reverse("customer_detail_by_user", args=[customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Customer User")
        self.assertContains(response, "Rental History")
        self.assertContains(response, rental.suit.suit_name)

    def test_create_cashier_rejects_short_passwords(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        self.client.login(username="admin", password="testpass123")

        response = self.client.post(
            reverse("create_cashier"),
            {
                "username": "cashier_short",
                "email": "cashier@example.com",
                "password": "abc",
                "password2": "abc",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("manage_users"))
        self.assertContains(response, "Password must be at least 4 characters")
        self.assertFalse(User.objects.filter(username="cashier_short").exists())

    def test_reception_form_creates_reception_account(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        self.client.login(username="admin", password="testpass123")

        response = self.client.get(reverse("reception_form"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Reception Account")

        post_response = self.client.post(
            reverse("reception_form"),
            {
                "username": "reception_user",
                "email": "reception@example.com",
                "password": "Reception123!",
                "password2": "Reception123!",
            },
            follow=True,
        )

        self.assertRedirects(post_response, reverse("manage_users"))
        self.assertContains(post_response, "Reception user 'reception_user' created successfully.")
        user = User.objects.get(username="reception_user")
        self.assertTrue(user.profile.is_reception)
        self.assertTrue(user.is_staff)

    def test_new_rent_for_customer_without_history_prefills_blank_values(self):
        admin = User.objects.create_user(username="admin", password="testpass123")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])
        admin_profile = admin.profile
        admin_profile.is_cashier = False
        admin_profile.is_reception = False
        admin_profile.save(update_fields=["is_cashier", "is_reception"])

        customer = User.objects.create_user(username="customer", password="testpass123")

        self.client.login(username="admin", password="testpass123")
        response = self.client.get(reverse("new_rent_for_customer", args=[customer.id]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Create New Rental Form", response.content.decode())
        # Verify that the prefilled customer ID is displayed in the context
        self.assertEqual(response.context["prefill_customer_id"], customer.id)


class PublicPagesDatabaseTests(TestCase):
    def test_contact_page_saves_message_to_database(self):
        response = self.client.post(
            reverse("contact_page"),
            {
                "name": "Ahmed Ali",
                "email": "ahmed@example.com",
                "phone": "+252611234567",
                "subject": "Booking Question",
                "message": "I would like to rent a suit for a wedding.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ContactMessage.objects.filter(email="ahmed@example.com").exists())
        saved_message = ContactMessage.objects.get(email="ahmed@example.com")
        self.assertEqual(saved_message.subject, "Booking Question")
        self.assertEqual(saved_message.message, "I would like to rent a suit for a wedding.")

    def test_contact_page_rejects_invalid_email_without_saving(self):
        response = self.client.post(
            reverse("contact_page"),
            {
                "name": "Ahmed Ali",
                "email": "not-an-email",
                "phone": "+252611234567",
                "subject": "Booking Question",
                "message": "I would like to rent a suit for a wedding.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContactMessage.objects.exists())

    def test_home_page_uses_database_counts_for_stats(self):
        before_total = Suit.objects.count()
        before_luxury = Suit.objects.filter(collection="Luxury").count()
        before_standard = Suit.objects.filter(collection="Standard").count()
        before_budget = Suit.objects.filter(collection="Budget").count()

        Suit.objects.create(suit_name="Luxury Test", price_per_day=Decimal("30.00"), quantity=2, collection="Luxury", status="Available")
        Suit.objects.create(suit_name="Standard Test", price_per_day=Decimal("20.00"), quantity=3, collection="Standard", status="Available")
        Suit.objects.create(suit_name="Budget Test", price_per_day=Decimal("15.00"), quantity=4, collection="Budget", status="Available")

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_suits"], before_total + 3)
        self.assertEqual(response.context["luxury_count"], before_luxury + 1)
        self.assertEqual(response.context["standard_count"], before_standard + 1)
        self.assertEqual(response.context["budget_count"], before_budget + 1)

    def test_public_booking_without_suit_id_redirects_with_error(self):
        customer = User.objects.create_user(username="customer-booking-empty", password="testpass123")
        self.client.login(username="customer-booking-empty", password="testpass123")

        response = self.client.post(
            reverse("book_suit"),
            {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "252611223344",
                "days_requested": "2",
                "quantity_requested": "1",
            },
            follow=True,
        )

        self.assertRedirects(response, reverse("all_suits"))
        self.assertContains(response, "Please select a suit")
        self.assertFalse(SuitRequest.objects.filter(user=customer).exists())


class InventoryManagementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="testpass123")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.profile = self.user.profile
        self.profile.is_cashier = False
        self.profile.is_reception = False
        self.profile.save(update_fields=["is_cashier", "is_reception"])

        Suit.objects.create(
            suit_name="Admin Suit",
            price_per_day=Decimal("30.00"),
            quantity=2,
            status="Available",
        )

    def test_approving_request_redirects_back_to_requests_list(self):
        self.reception_user = User.objects.create_user(username="reception", password="testpass123")
        self.reception_user.is_staff = True
        self.reception_user.save(update_fields=["is_staff"])
        self.reception_profile = self.reception_user.profile
        self.reception_profile.is_reception = True
        self.reception_profile.save(update_fields=["is_reception"])

        self.client.login(username="reception", password="testpass123")
        suit = Suit.objects.create(suit_name="Approval Suit", price_per_day=Decimal("15.00"), quantity=1, status="Available")
        req = SuitRequest.objects.create(
            suit=suit,
            name="Approval Customer",
            phone="252611222222",
            days_requested=2,
            quantity_requested=1,
            total_amount=Decimal("30.00"),
            status="Pending",
        )

        response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "start_date": "2026-07-04",
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
            },
        )

        self.assertRedirects(response, reverse("reception_requests"))

    def test_customer_history_page_shows_new_rent_button(self):
        self.customer = User.objects.create_user(username="customer", password="testpass123")
        self.client.login(username="customer", password="testpass123")

        response = self.client.get(reverse("customer_history"))

        self.assertContains(response, "New Rent")

    def test_booking_from_public_page_is_saved_and_shown_in_customer_history(self):
        customer = User.objects.create_user(username="customer-booking", password="testpass123", email="customer@example.com")
        self.client.login(username="customer-booking", password="testpass123")
        suit = Suit.objects.create(suit_name="Booking Suit", price_per_day=Decimal("30.00"), quantity=2, status="Available")

        response = self.client.post(
            reverse("book_suit"),
            {
                "suit_id": suit.id,
                "name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "252611223344",
                "days_requested": "2",
                "quantity_requested": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        booking = SuitRequest.objects.get(user=customer, suit=suit)
        self.assertEqual(booking.name, "Jane Doe")
        self.assertEqual(booking.customer_email, "jane@example.com")

        history_response = self.client.get(reverse("customer_history"))
        self.assertContains(history_response, "Jane Doe")
        self.assertContains(history_response, "Booking Suit")

    def test_request_form_accepts_full_name_address_and_image_upload(self):
        customer = User.objects.create_user(username="request-form-customer", password="testpass123", email="request@example.com")
        self.client.login(username="request-form-customer", password="testpass123")
        suit = Suit.objects.create(suit_name="Request Form Suit", price_per_day=Decimal("25.00"), quantity=2, status="Available")

        image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAIAAeIhvAAAAAElFTkSuQmCC")
        uploaded_image = SimpleUploadedFile("customer.png", image_bytes, content_type="image/png")

        response = self.client.post(
            reverse("request_suit", args=[suit.id]),
            {
                "full_name": "Jane Doe",
                "phone_number": "252611223344",
                "address": "Mogadishu",
                "days": "2",
                "quantity": "1",
                "customer_image": uploaded_image,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        booking = SuitRequest.objects.get(user=customer, suit=suit)
        self.assertEqual(booking.name, "Jane Doe")
        self.assertEqual(booking.phone, "252611223344")
        self.assertEqual(booking.present_address, "Mogadishu")
        self.assertEqual(booking.days_requested, 2)
        self.assertEqual(booking.quantity_requested, 1)
        self.assertTrue(booking.customer_image)

    def test_request_form_renders_customer_template_for_invalid_submission(self):
        customer = User.objects.create_user(username="request-form-invalid", password="testpass123", email="invalid@example.com")
        self.client.login(username="request-form-invalid", password="testpass123")
        suit = Suit.objects.create(suit_name="Invalid Request Suit", price_per_day=Decimal("25.00"), quantity=2, status="Available")

        response = self.client.post(
            reverse("request_suit", args=[suit.id]),
            {
                "full_name": "",
                "phone": "",
                "address": "",
                "days_requested": "2",
                "quantity_requested": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "customer/request_form.html")
        self.assertContains(response, "Please enter your full name")

    def test_request_form_accepts_common_image_extensions(self):
        customer = User.objects.create_user(username="request-form-image", password="testpass123", email="image@example.com")
        self.client.login(username="request-form-image", password="testpass123")
        suit = Suit.objects.create(suit_name="Image Upload Suit", price_per_day=Decimal("25.00"), quantity=2, status="Available")

        uploaded_image = SimpleUploadedFile("customer.jfif", b"fake-image-bytes", content_type="image/jpeg")

        response = self.client.post(
            reverse("request_suit", args=[suit.id]),
            {
                "full_name": "Jane Doe",
                "phone": "252611223344",
                "address": "Mogadishu",
                "days_requested": "2",
                "quantity_requested": "1",
                "customer_image": uploaded_image,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        booking = SuitRequest.objects.get(user=customer, suit=suit)
        self.assertTrue(booking.customer_image)

    def test_all_suits_page_links_available_suits_to_request_form(self):
        suit = Suit.objects.create(suit_name="Direct Booking Suit", price_per_day=Decimal("18.00"), quantity=2, status="Available")

        response = self.client.get(reverse("all_suits"))

        self.assertContains(response, f'/request-form/{suit.id}/')

    def test_approving_request_with_accessories_updates_total_and_status(self):
        self.client.login(username="reception", password="testpass123")
        suit = Suit.objects.create(suit_name="Accessory Suit", price_per_day=Decimal("25.00"), quantity=1, status="Available")
        req = SuitRequest.objects.create(
            suit=suit,
            name="Accessory Customer",
            phone="252611555555",
            days_requested=2,
            quantity_requested=1,
            total_amount=Decimal("50.00"),
            status="Pending",
        )

        response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "start_date": "2026-07-05",
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
                "accessories": ["shoes", "tie"],
                "shoes_size": "42",
                "shoes_color": "Black",
                "tie_size": "Slim",
                "tie_color": "Red",
            },
        )

        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "Active")
        self.assertEqual(req.total_amount, Decimal("50.00") + Decimal("5.00") * 2 + Decimal("1.50") * 2)
        self.assertIn("Accessories:", req.notes)
        self.assertIn("shoes", req.notes.lower())

    def test_admin_inventory_page_renders_professional_dashboard(self):
        self.client.login(username="admin", password="testpass123")
        response = self.client.get(reverse("inventory"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/inventory_admin.html")
        self.assertContains(response, "Inventory Management")
        self.assertContains(response, "Add Suit")
        self.assertContains(response, "Admin Suit")

    def test_admin_inventory_modal_uses_real_form_fields(self):
        self.client.login(username="admin", password="testpass123")
        response = self.client.get(reverse("inventory"))

        self.assertContains(response, 'name="suit_id"')
        self.assertContains(response, 'name="image"')
        self.assertContains(response, 'id="deleteSuitId"')

    def test_all_suits_page_filters_by_category(self):
        Suit.objects.create(suit_name="Category Suit Test", price_per_day=Decimal("35.00"), quantity=2, status="Available")
        Suit.objects.create(suit_name="Category Shirt Test", price_per_day=Decimal("15.00"), quantity=3, status="Available")

        response = self.client.get(reverse("all_suits"), {"category": "shirts"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_category"], "shirts")
        self.assertGreaterEqual(response.context["suits"].count(), 1)
        self.assertTrue(any(item.suit_name == "Category Shirt Test" for item in response.context["suits"]))

    def test_service_page_renders(self):
        response = self.client.get(reverse("service_page"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "rental/service.html")

    def test_contact_page_renders(self):
        response = self.client.get(reverse("contact_page"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "rental/contact.html")

    def test_about_page_renders(self):
        response = self.client.get(reverse("about_page"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "rental/about.html")

    def test_dashboard_chart_context_is_populated_for_selected_period(self):
        self.client.login(username="admin", password="testpass123")
        suit = Suit.objects.create(suit_name="Chart Suit", price_per_day=Decimal("40.00"), quantity=2, status="Available")
        SuitRequest.objects.create(
            suit=suit,
            name="Chart Customer",
            phone="252611333333",
            days_requested=2,
            quantity_requested=1,
            total_amount=Decimal("80.00"),
            status="Active",
        )

        response = self.client.get(reverse("dashboard"), {"period": "monthly", "category": "suits", "status": "rented"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_period"], "monthly")
        self.assertEqual(response.context["selected_category"], "suits")
        self.assertEqual(response.context["selected_status"], "rented")
        self.assertTrue(response.context["period_labels"])
        self.assertTrue(response.context["period_revenue_data"])
        self.assertTrue(response.context["top_suits_labels"])

    def test_dashboard_includes_recent_transactions(self):
        self.client.login(username="admin", password="testpass123")
        suit = Suit.objects.create(suit_name="Transaction Suit", price_per_day=Decimal("40.00"), quantity=2, status="Available")
        request = SuitRequest.objects.create(
            suit=suit,
            name="Transaction Customer",
            phone="252611444444",
            days_requested=1,
            quantity_requested=1,
            total_amount=Decimal("40.00"),
            status="Active",
        )
        from .models import Payment
        Payment.objects.create(
            rental=request,
            amount_paid=Decimal("40.00"),
            payment_method="cash",
            status="paid",
        )

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("recent_transactions", response.context)
        self.assertTrue(response.context["recent_transactions"].exists())

    def test_admin_booking_route_is_available(self):
        self.client.login(username="admin", password="testpass123")
        response = self.client.get(reverse("admin_booking"))

        self.assertEqual(response.status_code, 200)
