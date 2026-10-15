import base64
from datetime import datetime, time
from decimal import Decimal
from unittest import skip
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import ContactMessage, DryCleaning, Expense, InventoryMovement, LateFeeTransaction, Payment, Profile, RentalPolicy, Suit, SuitRequest


class BootstrapSuperuserCommandTests(TestCase):
    def test_creates_superuser_from_environment_and_is_idempotent(self):
        environment = {
            "BOOTSTRAP_SUPERUSER_USERNAME": "deploy-admin",
            "BOOTSTRAP_SUPERUSER_PASSWORD": "a-strong-temporary-password",
            "BOOTSTRAP_SUPERUSER_EMAIL": "admin@example.com",
        }
        with patch.dict("os.environ", environment):
            call_command("bootstrap_superuser", verbosity=0)
            call_command("bootstrap_superuser", verbosity=0)

        user = User.objects.get(username="deploy-admin")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("a-strong-temporary-password"))
        self.assertEqual(user.email, "admin@example.com")

    def test_requires_username_and_password(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(CommandError):
                call_command("bootstrap_superuser", verbosity=0)


class PublicViewsModuleTests(TestCase):
    def test_public_views_module_exports_core_views(self):
        from . import views_public

        self.assertTrue(callable(views_public.home))
        self.assertTrue(callable(views_public.contact_page))
        self.assertTrue(callable(views_public.customer_login))
        self.assertTrue(callable(views_public.login_view))

    def test_staff_login_page_is_available(self):
        response = self.client.get(reverse("admin_login"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Cashier")

    def test_reception_account_logs_in_to_customer_directory_only(self):
        user = User.objects.create_user(username="reception", password="testpass123", is_staff=True)
        user.profile.is_reception = True
        user.profile.save(update_fields=["is_reception"])

        response = self.client.post(reverse("admin_login"), {
            "username": "reception",
            "password": "testpass123",
        })

        self.assertRedirects(response, reverse("manage_users"))
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

    def test_reception_staff_and_payroll_routes_are_removed(self):
        for path in (
            "/reception-dashboard/",
            "/add-staff/",
            "/add-payroll/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_admin_user_page_hides_staff_and_payroll_creation(self):
        admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="testpass123",
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("manage_users"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add Staff")
        self.assertNotContains(response, "Add Payroll")

    def test_customer_login_session_expires_after_24_hours(self):
        User.objects.create_user(username="customer", password="testpass123")

        response = self.client.post(
            reverse("customer_login"),
            {"username": "customer", "password": "testpass123"},
        )

        self.assertRedirects(response, reverse("all_suits"))
        self.assertEqual(self.client.session.get_expiry_age(), 60 * 60 * 24)


@skip("Reception-specific workflows have been removed from the application.")
class ReceptionDashboardFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reception", password="testpass123")
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        admin_group, _ = Group.objects.get_or_create(name="Admin")
        self.user.groups.add(admin_group)
        self.profile = self.user.profile
        self.profile.is_reception = True
        self.profile.save(update_fields=["is_reception"])

        self.suit = Suit.objects.create(
            suit_name="Test Suit",
            price_per_day=Decimal("25.00"),
            quantity=3,
            status="Available",
        )
        self.active_rental = SuitRequest.objects.create(
            suit=self.suit,
            name="Test Customer",
            phone="252611111111",
            total_amount=Decimal("75.00"),
            status="Active",
            quantity_requested=1,
        )
        Expense.objects.create(description="Dry cleaning", amount=Decimal("12.50"), created_by=self.user)

    def test_partial_return_payment_is_recorded_without_completing_return(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("process_return"), {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "20.00",
        })

        self.active_rental.refresh_from_db()
        self.assertRedirects(response, reverse("reception_return", args=[self.active_rental.pk]))
        self.assertEqual(self.active_rental.status, "Active")
        self.assertEqual(self.active_rental.balance_due, Decimal("55.00"))
        self.assertEqual(Payment.objects.get(rental=self.active_rental).amount_paid, Decimal("20.00"))

    def test_return_cannot_record_zero_payment_while_balance_remains(self):
        self.client.login(username="reception", password="testpass123")

        response = self.client.post(reverse("process_return"), {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "0.00",
        })

        self.active_rental.refresh_from_db()
        self.assertRedirects(response, reverse("reception_return", args=[self.active_rental.pk]))
        self.assertEqual(self.active_rental.status, "Active")
        self.assertFalse(Payment.objects.filter(rental=self.active_rental).exists())

    def test_reception_requests_links_active_rentals_to_full_return_page(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("reception_requests"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("reception_return", args=[self.active_rental.pk])}"',
        )

        audit_response = self.client.get(reverse("reception_return", args=[self.active_rental.pk]))

        self.assertEqual(audit_response.status_code, 200)
        self.assertTemplateUsed(audit_response, "reception/reception_return.html")

    def test_return_page_prefills_the_selected_rental_and_uses_return_submission(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("reception_return", args=[self.active_rental.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="rental_id" value="{self.active_rental.pk}"')
        self.assertContains(response, f'action="{reverse("process_return")}"')
        self.assertContains(response, "Complete Return")

    def test_full_return_payment_completes_return_and_queues_suit(self):
        self.client.login(username="reception", password="testpass123")

        response = self.client.post(reverse("process_return"), {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "75.00",
        })

        self.active_rental.refresh_from_db()
        self.suit.refresh_from_db()
        self.assertRedirects(response, reverse("laundry_backlog"))
        self.assertEqual(self.active_rental.status, "Returned")
        self.assertEqual(self.active_rental.balance_due, Decimal("0.00"))
        self.assertEqual(self.suit.status, "Dry Cleaning")

    def test_process_return_records_late_fee_as_a_single_transaction(self):
        self.client.force_login(self.user)
        due_time = timezone.now() - timezone.timedelta(minutes=20)
        self.active_rental.due_date = due_time
        self.active_rental.end_time = due_time
        self.active_rental.save(update_fields=["due_date", "end_time"])

        response = self.client.post(reverse("process_return"), {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "75.50",
            "return_condition": "damaged",
        })

        fee = LateFeeTransaction.objects.get(rental=self.active_rental)
        self.active_rental.refresh_from_db()
        self.suit.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fee.amount, Decimal("0.50"))
        self.assertEqual(fee.assessed_by, self.user)
        self.assertEqual(LateFeeTransaction.objects.filter(rental=self.active_rental).count(), 1)
        self.assertEqual(self.active_rental.return_condition, "damaged")
        self.assertEqual(self.active_rental.returned_by, self.user)
        self.assertEqual(self.suit.status, "Dry Cleaning")

    def test_process_return_rejects_unknown_return_condition(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("process_return"), {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "75.00",
            "return_condition": "missing",
        })

        self.active_rental.refresh_from_db()
        self.assertRedirects(response, reverse("reception_return", args=[self.active_rental.pk]))
        self.assertEqual(self.active_rental.status, "Active")
        self.assertFalse(Payment.objects.filter(rental=self.active_rental).exists())

    def test_repeated_partial_return_payment_submission_is_idempotent(self):
        self.client.force_login(self.user)
        payment_key = "64e6ff3e-2fdd-4d9c-8fb0-0e4cd3e9a10a"
        return_data = {
            "rental_id": self.active_rental.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "20.00",
            "payment_idempotency_key": payment_key,
        }

        first_response = self.client.post(reverse("process_return"), return_data)
        second_response = self.client.post(reverse("process_return"), return_data)

        self.assertRedirects(first_response, reverse("reception_return", args=[self.active_rental.pk]))
        self.assertRedirects(second_response, reverse("reception_return", args=[self.active_rental.pk]))
        self.assertEqual(Payment.objects.filter(rental=self.active_rental).count(), 1)
        self.assertEqual(
            Payment.objects.get(rental=self.active_rental).idempotency_key,
            payment_key,
        )

    def test_reception_page_uses_reception_booking_template_with_live_context(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reception"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reception/reception.html")
        self.assertContains(response, "Available Suits Showroom")
        self.assertContains(response, "Dry Cleaning")
        self.assertContains(response, "3")

    def test_reception_page_renders_rental_duration_options(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("reception_reception"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 Day (24 Hours)")
        self.assertContains(response, "30 Days (720 Hours)")

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
            status="Pending",
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
        self.assertTrue(req.return_suit())
        self.assertFalse(req.return_suit())

        suit.refresh_from_db()
        self.assertEqual(suit.status, "Available")
        self.assertEqual(suit.quantity, 1)

    def test_repeated_return_submission_restores_accessory_stock_once(self):
        suit = Suit.objects.create(
            suit_name="Return Idempotency Shoes",
            price_per_day=Decimal("10.00"),
            quantity=0,
            status="Rented",
            item_type="shoes",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Return Idempotency Customer",
            phone="252611111110",
            total_amount=Decimal("20.00"),
            status="Active",
            quantity_requested=1,
            days_requested=2,
        )
        self.client.login(username="reception", password="testpass123")
        return_data = {
            "rental_id": req.id,
            "actual_return_date": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "return_amount_paid": "20.00",
        }

        first_response = self.client.post(reverse("process_return"), return_data)
        second_response = self.client.post(reverse("process_return"), return_data)

        self.assertRedirects(first_response, reverse("laundry_backlog"))
        self.assertRedirects(second_response, reverse("reception_requests"))
        suit.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(suit.quantity, 1)
        self.assertEqual(req.status, "Returned")
        self.assertEqual(Payment.objects.filter(rental=req).count(), 1)

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

    def test_issuing_same_rental_twice_decrements_stock_only_once(self):
        suit = Suit.objects.create(
            suit_name="Idempotent Issue Suit",
            price_per_day=Decimal("20.00"),
            quantity=2,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Issue Customer",
            phone="252611111119",
            status="Pending",
            quantity_requested=1,
            days_requested=1,
        )

        self.assertTrue(req.start_rental())
        self.assertFalse(req.start_rental())

        suit.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(suit.quantity, 1)
        self.assertEqual(suit.status, "Available")
        self.assertEqual(req.status, "Active")

    def test_issuing_more_than_available_stock_fails_without_changing_records(self):
        suit = Suit.objects.create(
            suit_name="Insufficient Stock Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Issue Customer",
            phone="252611111118",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )

        with self.assertRaises(ValidationError):
            req.start_rental()

        suit.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(suit.quantity, 1)
        self.assertEqual(req.status, "Pending")

    def test_quantity_remains_bookable_when_global_status_is_reserved(self):
        suit = Suit.objects.create(
            suit_name="Partial Reservation Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Reserved",
        )

        self.assertTrue(suit.is_available_for_booking)

    def test_reservation_holds_only_its_quantity(self):
        req = SuitRequest.objects.create(
            suit=self.suit,
            name="Reserved Customer",
            phone="252611111117",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )

        self.assertTrue(req.reserve_inventory(timezone.now() + timezone.timedelta(hours=24)))

        self.suit.refresh_from_db()
        self.assertEqual(self.suit.held_quantity, 2)
        self.assertEqual(self.suit.available_quantity, 1)
        self.assertEqual(self.suit.status, "Available")

    def test_reservation_cannot_exceed_unreserved_stock(self):
        first = SuitRequest.objects.create(
            suit=self.suit,
            name="First Reserved Customer",
            phone="252611111116",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )
        second = SuitRequest.objects.create(
            suit=self.suit,
            name="Second Reserved Customer",
            phone="252611111115",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )
        first.reserve_inventory(timezone.now() + timezone.timedelta(hours=24))

        with self.assertRaises(ValidationError):
            second.reserve_inventory(timezone.now() + timezone.timedelta(hours=24))

        second.refresh_from_db()
        self.assertEqual(second.status, "Pending")

    def test_issuing_unreserved_rental_respects_existing_reservations(self):
        reservation = SuitRequest.objects.create(
            suit=self.suit,
            name="Reserved Customer",
            phone="252611111114",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )
        reservation.reserve_inventory(timezone.now() + timezone.timedelta(hours=24))
        pending_rental = SuitRequest.objects.create(
            suit=self.suit,
            name="Walk-in Customer",
            phone="252611111113",
            status="Pending",
            quantity_requested=2,
            days_requested=1,
        )

        with self.assertRaises(ValidationError):
            pending_rental.start_rental()

        self.suit.refresh_from_db()
        self.assertEqual(self.suit.quantity, 3)

    def test_expired_reservation_no_longer_reduces_available_stock(self):
        SuitRequest.objects.create(
            suit=self.suit,
            name="Expired Reserved Customer",
            phone="252611111112",
            status="Reserved",
            quantity_requested=2,
            days_requested=1,
            due_date=timezone.now() - timezone.timedelta(minutes=1),
        )

        self.suit.refresh_from_db()
        self.assertEqual(self.suit.held_quantity, 0)
        self.assertEqual(self.suit.available_quantity, 3)

    def test_reception_available_inventory_filter_excludes_fully_held_stock(self):
        reservation = SuitRequest.objects.create(
            suit=self.suit,
            name="Fully Reserved Customer",
            phone="252611111109",
            status="Pending",
            quantity_requested=3,
            days_requested=1,
        )
        reservation.reserve_inventory(timezone.now() + timezone.timedelta(hours=24))
        self.client.force_login(self.user)

        response = self.client.get(reverse("reception_inventory"), {"status": "available"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.suit, response.context["suits"])

    def test_active_rental_charges_half_dollar_per_started_overdue_hour(self):
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

        self.assertEqual(req.late_fee, Decimal("0.50"))

    def test_partial_overdue_hour_is_rounded_up_to_a_full_hour(self):
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

        self.assertEqual(req.late_fee, Decimal("0.50"))

    def test_return_records_late_fee_once_with_assessing_staff_and_policy_snapshot(self):
        suit = Suit.objects.create(
            suit_name="Fee Transaction Shoes",
            item_type="shoes",
            price_per_day=Decimal("10.00"),
            quantity=0,
            status="Rented",
        )
        due_time = timezone.now() - timezone.timedelta(hours=1, minutes=5)
        req = SuitRequest.objects.create(
            suit=suit,
            name="Fee Transaction Customer",
            phone="252611112222",
            total_amount=Decimal("10.00"),
            status="Active",
            quantity_requested=1,
            days_requested=1,
            rental_start_time=due_time - timezone.timedelta(days=1),
            due_date=due_time,
            end_time=due_time,
        )

        self.assertTrue(req.return_suit(performed_by=self.user))
        self.assertFalse(req.return_suit(performed_by=self.user))

        fee = LateFeeTransaction.objects.get(rental=req)
        req.refresh_from_db()
        self.assertEqual(fee.amount, Decimal("1.00"))
        self.assertEqual(fee.hourly_rate, Decimal("0.50"))
        self.assertEqual(fee.partial_hour_policy, "round_up")
        self.assertEqual(fee.assessed_by, self.user)
        self.assertEqual(req.late_fee, fee.amount)
        self.assertEqual(LateFeeTransaction.objects.filter(rental=req).count(), 1)

    def test_dry_cleaning_ledger_shows_recent_queue_and_expenses(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("dry_cleaning_ledger"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cleaning Expense Ledger")
        self.assertContains(response, "Dry Cleaning Ledger")

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
                status="Returned",
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
        self.assertContains(response, "Pending Admin Approval")
        self.assertContains(response, "page=2")
        self.assertContains(response, "Request Laundry")

    def test_admin_laundry_backlog_uses_admin_template(self):
        User.objects.create_user(username="laundry_admin", password="testpass123", is_staff=True)
        self.client.login(username="laundry_admin", password="testpass123")

        response = self.client.get(reverse("laundry_backlog"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/loundary_admin.html")

    def test_laundry_backlog_renders_records_without_requester_or_approver(self):
        suit = Suit.objects.create(
            suit_name="Legacy Laundry Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        SuitRequest.objects.create(
            suit=suit,
            name="Legacy Customer",
            phone="252611111111",
            total_amount=Decimal("20.00"),
            status="Returned",
            quantity_requested=1,
        )
        DryCleaning.objects.create(
            suit=suit,
            amount=Decimal("5.00"),
            status="pending",
            requested_by=None,
            approved_by=None,
        )

        self.client.login(username="reception", password="testpass123")
        response = self.client.get(reverse("laundry_backlog"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Legacy Laundry Suit")
        self.assertContains(response, "Requested by Staff")

    def test_reception_laundry_request_waits_for_admin_approval(self):
        suit = Suit.objects.create(
            suit_name="Approval Required Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Laundry Customer",
            phone="252611111111",
            total_amount=Decimal("40.00"),
            status="Returned",
            quantity_requested=1,
        )

        self.client.login(username="reception", password="testpass123"        )

        response = self.client.post(reverse("mark_dry_cleaning", args=[suit.id]))

        suit.refresh_from_db()
        cleaning = DryCleaning.objects.get(suit=suit)
        self.assertRedirects(response, reverse("reception_inventory"))
        self.assertEqual(suit.status, "Returned")
        self.assertEqual(cleaning.status, "pending")
        self.assertEqual(cleaning.requested_by, self.user)
        self.assertEqual(cleaning.rental_request, rental)
        self.assertIsNotNone(cleaning.requested_at)

    def test_reception_inventory_offers_request_not_direct_dispatch(self):
        suit = Suit.objects.create(
            suit_name="Reception Inventory Laundry Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        self.client.login(username="reception", password="testpass123")

        response = self.client.get(reverse("reception_inventory"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request laundry approval")

    def test_reception_cannot_set_dry_cleaning_status_in_edit_form(self):
        suit = Suit.objects.create(
            suit_name="Reception Edit Laundry Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        self.client.login(username="reception", password="testpass123")

        response = self.client.post(reverse("edit_suit", args=[suit.id]), {"status": "Dry Cleaning"})

        suit.refresh_from_db()
        self.assertRedirects(response, reverse("reception_inventory"))
        self.assertEqual(suit.status, "Returned")

    def test_admin_approval_sends_reception_laundry_request(self):
        suit = Suit.objects.create(
            suit_name="Admin Approval Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Laundry Customer",
            phone="252611111111",
            total_amount=Decimal("40.00"),
            status="Returned",
            quantity_requested=1,
        )
        cleaning = DryCleaning.objects.create(
            suit=suit,
            rental_request=rental,
            requested_by=self.user,
            amount=Decimal("8.00"),
            status="pending",
        )
        admin = User.objects.create_user(username="laundry_admin", password="testpass123", is_staff=True)

        self.client.login(username="laundry_admin", password="testpass123")
        response = self.client.post(
            reverse("dry_cleaning"),
            {"action": "approve_request", "cleaning_id": cleaning.id},
        )

        cleaning.refresh_from_db()
        suit.refresh_from_db()
        self.assertRedirects(response, reverse("dry_cleaning"))
        self.assertEqual(cleaning.status, "sent_to_laundry")
        self.assertEqual(cleaning.approved_by, admin)
        self.assertIsNotNone(cleaning.approved_at)
        self.assertIsNotNone(cleaning.sent_to_laundry_at)
        self.assertEqual(suit.status, "Dry Cleaning")

    def test_reception_cannot_open_admin_laundry_approval_page(self):
        self.client.login(username="reception", password="testpass123")

        response = self.client.get(reverse("dry_cleaning"))

        self.assertRedirects(response, reverse("reception_dashboard"))

    def test_admin_laundry_page_shows_pending_request_audit(self):
        suit = Suit.objects.create(
            suit_name="Audit Laundry Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        DryCleaning.objects.create(
            suit=suit,
            requested_by=self.user,
            amount=Decimal("8.00"),
            status="pending",
        )
        User.objects.create_user(username="page_admin", password="testpass123", is_staff=True)
        admin = User.objects.get(username="page_admin")
        admin.is_staff = True
        admin.save(update_fields=["is_staff"])

        self.client.login(username="page_admin", password="testpass123")
        response = self.client.get(reverse("dry_cleaning"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Audit Laundry Suit")
        self.assertContains(response, "Approve and Send")
        self.assertContains(response, "reception")

    def test_admin_can_send_returned_suit_directly_to_laundry(self):
        suit = Suit.objects.create(
            suit_name="Direct Send Suit",
            price_per_day=Decimal("20.00"),
            quantity=0,
            status="Returned",
        )
        admin = User.objects.create_user(username="direct_admin", password="testpass123", is_staff=True)

        self.client.login(username="direct_admin", password="testpass123")
        response = self.client.post(reverse("mark_dry_cleaning", args=[suit.id]))

        cleaning = DryCleaning.objects.get(suit=suit)
        suit.refresh_from_db()
        self.assertRedirects(response, reverse("reception_inventory"))
        self.assertEqual(cleaning.status, "sent_to_laundry")
        self.assertEqual(cleaning.requested_by, admin)
        self.assertEqual(cleaning.approved_by, admin)
        self.assertIsNotNone(cleaning.sent_to_laundry_at)
        self.assertEqual(suit.status, "Dry Cleaning")

    def test_laundry_job_requires_each_stage_before_inventory_return(self):
        suit = Suit.objects.create(
            suit_name="Laundry Stages Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Dry Cleaning",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Laundry Customer",
            phone="252611111111",
            total_amount=Decimal("40.00"),
            status="Returned",
            quantity_requested=1,
        )
        cleaning = DryCleaning.objects.create(
            suit=suit,
            rental_request=rental,
            amount=Decimal("8.00"),
            status="sent_to_laundry",
        )
        self.client.login(username="reception", password="testpass123")

        response = self.client.post(
            reverse("laundry_backlog"),
            {"action": "start_laundry", "cleaning_id": cleaning.id},
        )
        cleaning.refresh_from_db()
        self.assertEqual(cleaning.status, "in_laundry")

        response = self.client.post(
            reverse("laundry_backlog"),
            {"action": "complete_laundry", "cleaning_id": cleaning.id},
        )
        cleaning.refresh_from_db()
        self.assertEqual(cleaning.status, "completed")
        self.assertIsNotNone(cleaning.completed_at)

        response = self.client.post(
            reverse("laundry_backlog"),
            {"action": "mark_ready", "request_id": rental.id, "cleaning_id": cleaning.id},
        )
        cleaning.refresh_from_db()
        suit.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(cleaning.status, "returned")
        self.assertIsNotNone(cleaning.returned_at)
        self.assertEqual(suit.status, "Available")
        self.assertEqual(suit.quantity, 2)
        movement = InventoryMovement.objects.get(
            suit=suit,
            rental_request=rental,
            movement_type="cleaning_completed",
        )
        self.assertEqual(movement.quantity_delta, 1)
        self.assertEqual(movement.quantity_after, 2)
        self.assertEqual(movement.performed_by, self.user)

        self.client.post(
            reverse("laundry_backlog"),
            {"action": "mark_ready", "request_id": rental.id, "cleaning_id": cleaning.id},
        )
        suit.refresh_from_db()
        self.assertEqual(suit.quantity, 2)
        self.assertEqual(
            InventoryMovement.objects.filter(
                suit=suit,
                rental_request=rental,
                movement_type="cleaning_completed",
            ).count(),
            1,
        )

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

        self.assertRedirects(response, reverse("reports"))
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

        self.assertRedirects(response, reverse("reception_dashboard"))

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

    def test_create_rental_uses_selected_days_for_return_time(self):
        self.client.login(username="reception", password="testpass123")
        response = self.client.post(
            reverse("create_rental"),
            {
                "suit_id": str(self.suit.id),
                "quantity": "1",
                "days": "2",
                "start_date": "2026-10-01",
                "end_date": "2026-10-08",
                "first_name": "Ahmed",
                "father_name": "Ali",
                "phone": "252611223344",
                "present_address": "Hargeisa",
                "amount_paid": "0",
            },
        )

        self.assertRedirects(response, reverse("reception_reception"))
        rental = SuitRequest.objects.get(name="Ahmed Ali")
        self.assertEqual(rental.days_requested, 2)
        self.assertEqual(rental.end_time - rental.rental_start_time, timezone.timedelta(hours=48))
        self.suit.refresh_from_db()
        self.assertEqual(self.suit.quantity, 2)

    def test_approve_request_does_not_start_rental_timer_or_decrement_stock(self):
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
                "action_mode": "rent_now",
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
                "start_date": "",
            },
            follow=True,
        )

        req.refresh_from_db()
        suit.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(req.rent_days, 2)
        self.assertIsNotNone(req.approved_at)
        self.assertEqual(req.status, "Approved")
        self.assertIsNone(req.rental_start_time)
        self.assertIsNone(req.due_date)
        self.assertEqual(suit.quantity, 1)

    def test_pickup_mode_opens_issue_action_by_default(self):
        req = SuitRequest.objects.create(
            suit=self.suit,
            name="Pickup Mode Customer",
            phone="252611223344",
            total_amount=Decimal("50.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=2,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("approve_request", args=[req.id]) + "?mode=pickup"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<option value="rent_now" selected>Issue to Customer Now</option>',
        )

    def test_accessories_and_price_are_preserved_until_issue(self):
        suit = Suit.objects.create(
            suit_name="Accessory Approval Suit",
            price_per_day=Decimal("20.00"),
            quantity=1,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Accessory Customer",
            phone="252611223344",
            total_amount=Decimal("40.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=2,
        )
        self.client.force_login(self.user)

        self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "approve",
                "amount_paid": "0",
                "payment_method": "cash",
                "accessories": ["shoes"],
                "shoes_size": "42",
                "shoes_color": "Brown",
            },
        )
        req.refresh_from_db()
        self.assertEqual(req.total_amount, Decimal("50.00"))
        self.assertEqual(req.accessory_items, ["shoes (42 / Brown)"])

        form_response = self.client.get(reverse("approve_request", args=[req.id]))
        self.assertContains(form_response, 'value="shoes" id="acc_shoes"')
        self.assertContains(form_response, 'data-price="5.00" checked')

        self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "rent_now",
                "amount_paid": "0",
                "payment_method": "cash",
                "accessories_configured": "1",
                "accessories": ["shoes"],
                "shoes_size": "",
                "shoes_color": "Black",
            },
        )
        req.refresh_from_db()
        self.assertEqual(req.status, "Active")
        self.assertEqual(req.total_amount, Decimal("50.00"))
        self.assertEqual(req.accessory_items, ["shoes (42 / Brown)"])

    def test_approved_request_starts_rental_only_when_issued(self):
        suit = Suit.objects.create(
            suit_name="Issue on Pickup Suit",
            price_per_day=Decimal("20.00"),
            quantity=2,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Pickup Customer",
            phone="252611223344",
            total_amount=Decimal("40.00"),
            status="Approved",
            quantity_requested=1,
            days_requested=2,
        )
        self.client.force_login(self.user)
        before_issue = timezone.now()

        response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "rent_now",
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
            },
        )

        req.refresh_from_db()
        suit.refresh_from_db()
        self.assertRedirects(response, reverse("requests"))
        self.assertEqual(req.status, "Active")
        self.assertGreaterEqual(req.rental_start_time, before_issue)
        self.assertEqual(req.due_date - req.rental_start_time, timezone.timedelta(days=2))
        self.assertEqual(suit.quantity, 1)
        self.assertTrue(
            InventoryMovement.objects.filter(
                rental_request=req,
                movement_type="issued",
                quantity_delta=-1,
            ).exists()
        )

    def test_reserved_request_can_be_issued_at_pickup(self):
        suit = Suit.objects.create(
            suit_name="Reserved Pickup Suit",
            price_per_day=Decimal("20.00"),
            quantity=2,
            status="Available",
        )
        req = SuitRequest.objects.create(
            suit=suit,
            name="Reserved Customer",
            phone="252611223344",
            total_amount=Decimal("40.00"),
            status="Pending",
            quantity_requested=1,
            days_requested=2,
        )
        self.client.force_login(self.user)

        reserve_response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "reserve",
                "amount_paid": "0",
                "reserve_duration": "24",
                "payment_method": "cash",
                "payment_reference": "",
            },
        )
        req.refresh_from_db()
        suit.refresh_from_db()
        self.assertRedirects(reserve_response, reverse("reception_requests"))
        self.assertEqual(req.status, "Reserved")
        self.assertIsNone(req.rental_start_time)
        self.assertEqual(suit.quantity, 2)

        issue_response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "rent_now",
                "amount_paid": "0",
                "payment_method": "cash",
                "payment_reference": "",
            },
        )

        req.refresh_from_db()
        suit.refresh_from_db()
        self.assertRedirects(issue_response, reverse("reception_requests"))
        self.assertEqual(req.status, "Active")
        self.assertIsNotNone(req.rental_start_time)
        self.assertEqual(suit.quantity, 1)

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

    def test_admin_requests_page_renders_the_hidden_return_panel(self):
        User.objects.create_user(username="return_admin", password="testpass123", is_staff=True)
        self.client.login(username="return_admin", password="testpass123")

        response = self.client.get(reverse("requests"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/admin_requests.html")
        self.assertContains(response, 'id="returnPanelSection"')
        self.assertContains(response, 'type="datetime-local"')
        self.assertContains(response, "Amount Received")

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


class AdminRequestsPageTests(TestCase):
    def test_admin_requests_tabs_search_and_paginate(self):
        admin = User.objects.create_superuser(
            username="requests_admin",
            email="requests@example.com",
            password="testpass123",
        )
        self.client.force_login(admin)
        for index in range(12):
            suit = Suit.objects.create(
                suit_name=f"Request Suit {index}",
                price_per_day=Decimal("20.00"),
                quantity=2,
                status="Available",
            )
            SuitRequest.objects.create(
                suit=suit,
                name=f"Request Customer {index}",
                phone="252611111111",
                total_amount=Decimal("20.00"),
                status="Pending",
            )

        first_page = self.client.get(reverse("requests"), {"tab": "pending"})
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context["page_obj"].paginator.count, 12)
        self.assertEqual(len(first_page.context["requests"]), 10)
        self.assertContains(first_page, "page=2")
        self.assertContains(first_page, 'id="returnPanelSection"')
        self.assertContains(first_page, "Amount Received")

        search_response = self.client.get(
            reverse("requests"),
            {"tab": "pending", "q": "Request Customer 11"},
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.context["page_obj"].paginator.count, 1)
        self.assertContains(search_response, "Request Customer 11")
        self.assertNotContains(search_response, "Request Customer 10")

    def test_rejecting_reserved_request_records_reason_and_releases_hold(self):
        admin = User.objects.create_superuser(
            username="reject_admin",
            email="reject@example.com",
            password="testpass123",
        )
        suit = Suit.objects.create(
            suit_name="Reserved Suit",
            price_per_day=Decimal("25.00"),
            quantity=1,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Reserved Customer",
            phone="252611111111",
            total_amount=Decimal("25.00"),
            status="Pending",
        )
        rental.reserve_inventory(timezone.now() + timezone.timedelta(hours=1), admin)
        self.client.force_login(admin)

        response = self.client.post(
            reverse("reject_request", args=[rental.pk]),
            {"reason": "Customer cancelled"},
        )

        rental.refresh_from_db()
        suit.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(rental.status, "Rejected")
        self.assertEqual(rental.rejected_reason, "Customer cancelled")
        self.assertEqual(rental.workflow_status, "cancelled")
        self.assertEqual(suit.status, "Available")
        self.assertRedirects(response, f"{reverse('requests')}?tab=closed")

    def test_reject_request_requires_post(self):
        admin = User.objects.create_superuser(
            username="reject_method_admin",
            email="reject-method@example.com",
            password="testpass123",
        )
        suit = Suit.objects.create(
            suit_name="Pending Suit",
            price_per_day=Decimal("25.00"),
            quantity=1,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Pending Customer",
            phone="252611111111",
            total_amount=Decimal("25.00"),
            status="Pending",
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("reject_request", args=[rental.pk]))

        rental.refresh_from_db()
        self.assertEqual(response.status_code, 405)
        self.assertEqual(rental.status, "Pending")

    def test_requests_page_expires_unpaid_reservations_and_releases_inventory(self):
        from .workflow import expire_overdue

        suit = Suit.objects.create(
            suit_name="Expired Hold Suit",
            price_per_day=Decimal("25.00"),
            quantity=1,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Expired Hold Customer",
            phone="252611111111",
            total_amount=Decimal("25.00"),
            status="Pending",
        )
        rental.reserve_inventory(timezone.now() - timezone.timedelta(minutes=1))

        expire_overdue()

        rental.refresh_from_db()
        suit.refresh_from_db()
        self.assertEqual(rental.status, "Expired")
        self.assertEqual(rental.workflow_status, "expired")
        self.assertEqual(suit.status, "Available")


class AdminUserManagementTests(TestCase):
    def create_admin(self):
        admin = User.objects.create_user(username="profile_admin", password="testpass123")
        admin.is_staff = True
        admin.is_superuser = True
        admin.save(update_fields=["is_staff", "is_superuser"])
        return admin

    def test_manage_users_links_to_full_profile_editor(self):
        admin = self.create_admin()
        customer = User.objects.create_user(username="profile_customer", password="testpass123")
        self.client.force_login(admin)

        response = self.client.get(reverse("manage_users"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("edit_user", args=[customer.pk]))

    def test_admin_can_edit_customer_name_login_email_and_password(self):
        admin = self.create_admin()
        customer = User.objects.create_user(username="before_edit", password="old-password")
        self.client.force_login(admin)

        response = self.client.post(reverse("edit_user", args=[customer.pk]), {
            "username": "updated_user",
            "first_name": "Ayaan",
            "last_name": "Ali",
            "email": "ayaan@example.com",
            "customer_phone": "252611223344",
            "national_id": "ID-12345",
            "present_address": "Hargeisa",
            "parents_address": "Mogadishu",
            "role": "customer",
            "is_active": "true",
            "new_password": "new-password",
            "password_confirmation": "new-password",
        }, follow=True)

        customer.refresh_from_db()
        self.assertRedirects(response, reverse("manage_users"))
        self.assertEqual(customer.username, "updated_user")
        self.assertEqual(customer.first_name, "Ayaan")
        self.assertEqual(customer.last_name, "Ali")
        self.assertEqual(customer.email, "ayaan@example.com")
        self.assertFalse(customer.is_staff)
        self.assertFalse(customer.profile.is_reception)
        self.assertFalse(customer.profile.is_cashier)
        self.assertEqual(customer.profile.customer_phone, "252611223344")
        self.assertEqual(customer.profile.national_id, "ID-12345")
        self.assertEqual(customer.profile.present_address, "Hargeisa")
        self.assertEqual(customer.profile.parents_address, "Mogadishu")
        self.assertTrue(customer.check_password("new-password"))

    def test_profile_editor_rejects_admin_role_escalation(self):
        admin = self.create_admin()
        customer = User.objects.create_user(username="ordinary_customer", password="testpass123")
        self.client.force_login(admin)

        response = self.client.post(reverse("edit_user", args=[customer.pk]), {
            "username": customer.username,
            "role": "admin",
            "is_active": "true",
        }, follow=True)

        customer.refresh_from_db()
        self.assertRedirects(response, reverse("edit_user", args=[customer.pk]))
        self.assertContains(response, "Choose a valid account role.")
        self.assertFalse(customer.is_staff)
        self.assertFalse(customer.profile.is_reception)
        self.assertFalse(customer.profile.is_cashier)

    def test_profile_editor_keeps_protected_admin_access_unchanged(self):
        admin = self.create_admin()
        protected_admin = User.objects.create_user(
            username="protected_editor_target",
            password="testpass123",
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )
        self.client.force_login(admin)

        response = self.client.post(reverse("edit_user", args=[protected_admin.pk]), {
            "username": protected_admin.username,
            "first_name": "Updated",
            "last_name": "Administrator",
            "email": "updated-admin@example.com",
            "role": "customer",
            "is_active": "false",
        }, follow=True)

        protected_admin.refresh_from_db()
        self.assertRedirects(response, reverse("manage_users"))
        self.assertEqual(protected_admin.first_name, "Updated")
        self.assertEqual(protected_admin.email, "updated-admin@example.com")
        self.assertTrue(protected_admin.is_superuser)
        self.assertTrue(protected_admin.is_staff)
        self.assertTrue(protected_admin.is_active)

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
        self.assertNotContains(response, "Add Staff")
        self.assertNotContains(response, "Add Payroll")

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
        customer.profile.customer_phone = "252611223344"
        customer.profile.national_id = "ID-12345"
        customer.profile.present_address = "Hargeisa"
        customer.profile.parents_address = "Mogadishu"
        customer.profile.save()
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
        self.assertContains(response, "Complete Rental History")
        self.assertContains(response, rental.suit.suit_name)
        self.assertContains(response, "Hargeisa")
        self.assertContains(response, "Mogadishu")

    @skip("Cashier account creation has been removed.")
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

    @skip("Reception account creation has been removed.")
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
        self.assertIn("New Rental for", response.content.decode())
        self.assertNotContains(response, "Complete Rental History")
        self.assertNotContains(response, "Total Rentals")
        # Verify that the prefilled customer ID is displayed in the context
        self.assertEqual(response.context["prefill_customer_id"], customer.id)

    def test_new_rent_for_customer_prefills_persistent_profile_details(self):
        admin = User.objects.create_user(username="prefill_admin", password="testpass123", is_staff=True)
        self.client.force_login(admin)
        customer = User.objects.create_user(
            username="profile_prefill_customer",
            password="testpass123",
            first_name="Ayaan",
            last_name="Ali",
        )
        customer.profile.customer_phone = "252611223344"
        customer.profile.national_id = "ID-12345"
        customer.profile.present_address = "Hargeisa"
        customer.profile.parents_address = "Mogadishu"
        customer.profile.save()

        response = self.client.get(
            reverse("new_rent_for_customer", args=[customer.pk]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["prefill_phone"], "252611223344")
        self.assertEqual(response.context["prefill_present_address"], "Hargeisa")
        self.assertEqual(response.context["prefill_national_id"], "ID-12345")
        self.assertEqual(response.context["prefill_parents_address"], "Mogadishu")


class RepeatRentalWorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="repeat_admin", is_staff=True)
        self.admin.profile.is_reception = False
        self.admin.profile.is_cashier = False
        self.admin.profile.save(update_fields=["is_reception", "is_cashier"])

        self.reception = User.objects.create_user(username="repeat_reception", is_staff=True)
        self.reception.profile.is_reception = True
        self.reception.profile.save(update_fields=["is_reception"])

        self.customer = User.objects.create_user(
            username="repeat_customer",
            first_name="Ayaan",
            last_name="Ali",
        )
        self.customer.profile.customer_phone = "252611223344"
        self.customer.profile.present_address = "Hargeisa"
        self.customer.profile.save(update_fields=["customer_phone", "present_address"])

        self.previous_suit = Suit.objects.create(
            suit_name="Previous Rental Suit",
            price_per_day=Decimal("20.00"),
            quantity=2,
            status="Rented",
        )
        self.new_suit = Suit.objects.create(
            suit_name="Repeat Rental Suit",
            price_per_day=Decimal("20.00"),
            quantity=3,
            status="Available",
        )
        due_date = timezone.now() - timezone.timedelta(days=1)
        self.previous_rental = SuitRequest.objects.create(
            user=self.customer,
            suit=self.previous_suit,
            name="Ayaan Ali",
            first_name="Ayaan",
            second_name="Ali",
            phone="252611223344",
            total_amount=Decimal("60.00"),
            days_requested=3,
            quantity_requested=1,
            status="Active",
            rental_start_time=due_date - timezone.timedelta(days=3),
            due_date=due_date,
            end_time=due_date,
        )
        Payment.objects.create(
            rental=self.previous_rental,
            amount_paid=Decimal("10.00"),
            payment_method="Cash",
        )
        self.client.force_login(self.admin)

    def test_repeat_rental_reuses_customer_and_keeps_history_and_payments_separate(self):
        history_response = self.client.get(
            reverse("new_rent_for_customer", args=[self.customer.pk])
        )

        self.assertEqual(history_response.status_code, 200)
        self.assertContains(history_response, "Previous Rental Suit")
        self.assertContains(history_response, "Overdue")
        self.assertContains(history_response, "Outstanding payment")
        self.assertContains(history_response, "A new rental is still allowed")
        self.assertContains(history_response, 'name="suit_price"')
        self.assertEqual(history_response.context["total_rentals"], 1)
        self.assertEqual(history_response.context["total_balance_due"], Decimal("50.00"))

        original_total = self.previous_rental.total_amount
        original_status = self.previous_rental.status
        original_return_date = self.previous_rental.return_date
        original_customer_count = User.objects.count()
        today = timezone.localdate()
        post_data = {
            "repeat_rental_submission": "1",
            "customer_user_id": str(self.customer.pk),
            "rental_submission_token": history_response.context["rental_submission_token"],
            "first_name": "Ayaan",
            "father_name": "Ali",
            "phone": "252611223344",
            "present_address": "Hargeisa",
            "suit_id": str(self.new_suit.pk),
            "suit_price": "13.00",
            "quantity": "1",
            "days": "2",
            "start_date": today.isoformat(),
            "end_date": (today + timezone.timedelta(days=2)).isoformat(),
            "amount_paid": "5.00",
            "payment_method": "cash",
        }

        response = self.client.post(reverse("create_rental"), post_data)

        self.assertRedirects(
            response,
            reverse("customer_detail_by_user", args=[self.customer.pk]),
        )
        self.previous_rental.refresh_from_db()
        self.new_suit.refresh_from_db()
        new_rental = SuitRequest.objects.get(user=self.customer, suit=self.new_suit)
        self.assertEqual(User.objects.count(), original_customer_count)
        self.assertEqual(SuitRequest.objects.filter(user=self.customer).count(), 2)
        self.assertEqual(self.previous_rental.total_amount, original_total)
        self.assertEqual(self.previous_rental.status, original_status)
        self.assertEqual(self.previous_rental.return_date, original_return_date)
        self.assertEqual(self.previous_rental.balance_due, Decimal("50.00"))
        self.assertEqual(new_rental.status, "Active")
        self.assertEqual(new_rental.suit_price_per_day, Decimal("13.00"))
        self.assertEqual(new_rental.booked_suit_price, Decimal("13.00"))
        self.assertEqual(new_rental.rental_subtotal, Decimal("26.00"))
        self.assertEqual(new_rental.balance_due, Decimal("21.00"))
        self.new_suit.price_per_day = Decimal("99.00")
        self.new_suit.save(update_fields=["price_per_day"])
        new_rental.refresh_from_db()
        self.assertEqual(new_rental.booked_suit_price, Decimal("13.00"))
        self.assertEqual(new_rental.rental_subtotal, Decimal("26.00"))
        self.assertEqual(Payment.objects.filter(rental=self.previous_rental).count(), 1)
        self.assertEqual(Payment.objects.filter(rental=new_rental).count(), 1)
        self.assertEqual(Payment.objects.get(rental=new_rental).amount_paid, Decimal("5.00"))
        self.assertEqual(self.new_suit.quantity, 2)

        duplicate_response = self.client.post(reverse("create_rental"), post_data)
        self.assertRedirects(
            duplicate_response,
            reverse("new_rent_for_customer", args=[self.customer.pk]),
        )
        self.assertEqual(SuitRequest.objects.filter(user=self.customer).count(), 2)

    def test_reception_can_use_customer_rental_flow_without_admin_controls(self):
        self.client.force_login(self.reception)

        directory_response = self.client.get(reverse("manage_users"))
        detail_response = self.client.get(
            reverse("customer_detail_by_user", args=[self.customer.pk])
        )
        new_rental_response = self.client.get(
            reverse("new_rent_for_customer", args=[self.customer.pk])
        )

        self.assertEqual(directory_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(new_rental_response.status_code, 200)
        self.assertContains(directory_response, "repeat_customer")
        self.assertNotContains(directory_response, "edit_user/")
        self.assertNotContains(directory_response, "toggle-user-active/")
        self.assertNotContains(directory_response, "deleteModal")
        self.assertContains(new_rental_response, "Create Rental")
        self.assertNotContains(new_rental_response, "Admin Dashboard")
        self.assertNotContains(new_rental_response, "Inventory")
        self.assertEqual(
            self.client.get(reverse("admin_booking")).status_code,
            302,
        )

    def test_staff_account_with_rental_history_can_repeat_rent(self):
        staff_customer = User.objects.create_user(
            username="staff_with_rentals",
            is_staff=True,
        )
        SuitRequest.objects.create(
            user=staff_customer,
            suit=self.previous_suit,
            name="Staff Rental Customer",
            phone="252611223344",
            total_amount=Decimal("20.00"),
            days_requested=1,
            quantity_requested=1,
            status="Returned",
        )

        form_response = self.client.get(
            reverse("new_rent_for_customer", args=[staff_customer.pk])
        )
        self.assertEqual(form_response.status_code, 200)
        self.assertEqual(form_response.context["customer_user"], staff_customer)

        today = timezone.localdate()
        response = self.client.post(
            reverse("create_rental"),
            {
                "repeat_rental_submission": "1",
                "customer_user_id": str(staff_customer.pk),
                "rental_submission_token": form_response.context["rental_submission_token"],
                "first_name": "Staff",
                "father_name": "Customer",
                "phone": "252611223344",
                "present_address": "Hargeisa",
                "suit_id": str(self.new_suit.pk),
                "quantity": "1",
                "days": "1",
                "start_date": today.isoformat(),
                "end_date": (today + timezone.timedelta(days=1)).isoformat(),
                "amount_paid": "0",
                "payment_method": "cash",
            },
        )

        self.assertRedirects(
            response,
            reverse("customer_detail_by_user", args=[staff_customer.pk]),
        )
        new_rental = SuitRequest.objects.get(user=staff_customer, suit=self.new_suit)
        self.assertEqual(new_rental.status, "Active")
        self.assertEqual(
            self.client.get(
                reverse("customer_detail_by_user", args=[staff_customer.pk])
            ).status_code,
            200,
        )

    def test_repeat_rental_rejects_unavailable_suit_without_mutating_customer_history(self):
        self.client.force_login(self.reception)
        form_response = self.client.get(
            reverse("new_rent_for_customer", args=[self.customer.pk])
        )
        unavailable_suit = Suit.objects.create(
            suit_name="Unavailable Repeat Suit",
            price_per_day=Decimal("30.00"),
            quantity=0,
            status="Rented",
        )
        today = timezone.localdate()

        response = self.client.post(
            reverse("create_rental"),
            {
                "repeat_rental_submission": "1",
                "customer_user_id": str(self.customer.pk),
                "rental_submission_token": form_response.context["rental_submission_token"],
                "first_name": "Ayaan",
                "father_name": "Ali",
                "phone": "252611223344",
                "suit_id": str(unavailable_suit.pk),
                "quantity": "1",
                "days": "1",
                "start_date": today.isoformat(),
                "end_date": (today + timezone.timedelta(days=1)).isoformat(),
                "amount_paid": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only 0 units")
        self.assertEqual(SuitRequest.objects.filter(user=self.customer).count(), 1)
        self.previous_rental.refresh_from_db()
        self.assertEqual(self.previous_rental.status, "Active")
        self.assertEqual(unavailable_suit.quantity, 0)

    def test_receipt_uses_existing_back_routes_for_admin_and_customer(self):
        self.client.force_login(self.admin)
        admin_response = self.client.get(reverse("receipt", args=[self.previous_rental.pk]))

        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, reverse("requests"))

        self.client.force_login(self.customer)
        customer_response = self.client.get(reverse("receipt", args=[self.previous_rental.pk]))

        self.assertEqual(customer_response.status_code, 200)
        self.assertContains(customer_response, reverse("customer_history"))
        self.assertNotContains(customer_response, "Admin Dashboard")
        self.assertContains(customer_response, f"SR-{self.previous_rental.pk:06d}")


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


class HistoricalDateWorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="historical_admin",
            email="historical@example.com",
            password="testpass123",
        )
        self.suit = Suit.objects.create(
            suit_name="Historical Test Suit",
            price_per_day=Decimal("25.00"),
            quantity=2,
            status="Available",
        )
        self.client.login(username="historical_admin", password="testpass123")

    def test_report_expense_date_field_accepts_a_historical_date(self):
        expense_date = timezone.localdate() - timezone.timedelta(days=2)

        response = self.client.post(reverse("add_expense"), {
            "amount": "12.50",
            "category": "Supplies",
            "description": "Historical supplies purchase",
            "expense_date": expense_date.isoformat(),
        })

        self.assertRedirects(response, reverse("reports"))
        self.assertEqual(timezone.localtime(Expense.objects.get().expense_date).date(), expense_date)

    def test_future_expense_date_is_rejected(self):
        future_date = timezone.localdate() + timezone.timedelta(days=1)

        response = self.client.post(reverse("add_expense"), {
            "amount": "12.50",
            "category": "Supplies",
            "description": "Future supplies purchase",
            "expense_date": future_date.isoformat(),
        }, follow=True)

        self.assertRedirects(response, reverse("reports"))
        self.assertFalse(Expense.objects.exists())
        self.assertContains(response, "cannot be in the future")

    def test_future_report_date_shows_error_without_falling_back_to_all_records(self):
        SuitRequest.objects.create(
            suit=self.suit,
            name="Report Customer",
            phone="252611111111",
            status="Returned",
            total_amount=Decimal("25.00"),
        )
        future_date = timezone.localdate() + timezone.timedelta(days=1)

        response = self.client.get(reverse("reports"), {"date": future_date.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_requests"], 0)
        self.assertContains(response, "cannot be in the future")

    def test_custom_report_range_filters_summary_and_export_consistently(self):
        today = timezone.localdate()
        end_of_day = timezone.make_aware(
            datetime.combine(today, time.max),
            timezone.get_current_timezone(),
        )
        included_rental = SuitRequest.objects.create(
            suit=self.suit,
            name="Included Report Customer",
            phone="252611111111",
            status="Returned",
            total_amount=Decimal("45.00"),
            request_date=end_of_day,
        )
        excluded_rental = SuitRequest.objects.create(
            suit=self.suit,
            name="Excluded Report Customer",
            phone="252611111112",
            status="Returned",
            total_amount=Decimal("100.00"),
            request_date=end_of_day - timezone.timedelta(days=1),
        )
        Payment.objects.create(
            rental=included_rental,
            amount_paid=Decimal("20.00"),
            payment_date=end_of_day,
        )
        Payment.objects.create(
            rental=excluded_rental,
            amount_paid=Decimal("90.00"),
            payment_date=end_of_day - timezone.timedelta(days=1),
        )
        Expense.objects.create(
            description="Included expense",
            amount=Decimal("5.00"),
            expense_date=end_of_day,
        )
        Expense.objects.create(
            description="Excluded expense",
            amount=Decimal("30.00"),
            expense_date=end_of_day - timezone.timedelta(days=1),
        )
        query = {"start_date": today.isoformat(), "end_date": today.isoformat()}

        page = self.client.get(reverse("reports"), query)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["revenue"], Decimal("45.00"))
        self.assertEqual(page.context["total_expenses"], Decimal("5.00"))
        self.assertEqual(page.context["net_profit"], Decimal("40.00"))
        self.assertEqual(page.context["total_rentals"], 1)
        self.assertContains(page, "Included Report Customer")
        self.assertNotContains(page, "Excluded Report Customer")

        exported = self.client.get(reverse("download_report"), query)
        self.assertEqual(exported.status_code, 200)
        self.assertIn(
            f"report_{today.isoformat()}_to_{today.isoformat()}.csv",
            exported["Content-Disposition"],
        )
        csv_text = exported.content.decode("utf-8-sig")
        self.assertIn("Total revenue,45.00", csv_text)
        self.assertIn("Total expenses,5.00", csv_text)
        self.assertIn("Net profit,40.00", csv_text)
        self.assertIn("Number of rentals,1", csv_text)
        self.assertIn("Included Report Customer", csv_text)
        self.assertNotIn("Excluded Report Customer", csv_text)
        self.assertIn("Included expense", csv_text)
        self.assertNotIn("Excluded expense", csv_text)

    def test_reports_default_to_current_month_and_keep_selected_dates(self):
        today = timezone.localdate()
        default_response = self.client.get(reverse("reports"))
        self.assertEqual(default_response.context["start_date"], today.replace(day=1).isoformat())
        self.assertEqual(default_response.context["end_date"], today.isoformat())

        selected_start = today - timezone.timedelta(days=2)
        selected_end = today - timezone.timedelta(days=1)
        selected_response = self.client.get(reverse("reports"), {
            "start_date": selected_start.isoformat(),
            "end_date": selected_end.isoformat(),
        })
        self.assertEqual(selected_response.context["start_date"], selected_start.isoformat())
        self.assertEqual(selected_response.context["end_date"], selected_end.isoformat())

    def test_incomplete_and_invalid_report_ranges_are_rejected(self):
        today = timezone.localdate()
        incomplete = self.client.get(reverse("reports"), {"start_date": today.isoformat()})
        self.assertEqual(incomplete.context["total_rentals"], 0)
        self.assertContains(incomplete, "Both start and end dates are required")
        self.assertEqual(
            self.client.get(reverse("download_report"), {"start_date": today.isoformat()}).status_code,
            400,
        )

        future = today + timezone.timedelta(days=1)
        invalid = self.client.get(reverse("reports"), {
            "start_date": today.isoformat(),
            "end_date": future.isoformat(),
        })
        self.assertContains(invalid, "End date cannot be in the future")
        reversed_range = self.client.get(reverse("reports"), {
            "start_date": today.isoformat(),
            "end_date": (today - timezone.timedelta(days=1)).isoformat(),
        })
        self.assertContains(reversed_range, "Start date must be on or before the end date")

    def test_admin_payment_revenue_respects_selected_historical_range(self):
        rental = SuitRequest.objects.create(
            suit=self.suit,
            name="Payment Customer",
            phone="252611111111",
            status="Returned",
            total_amount=Decimal("50.00"),
        )
        selected_date = timezone.localdate() - timezone.timedelta(days=5)
        Payment.objects.create(
            rental=rental,
            amount_paid=Decimal("10.00"),
            payment_date=timezone.now() - timezone.timedelta(days=5),
        )
        Payment.objects.create(
            rental=rental,
            amount_paid=Decimal("30.00"),
            payment_date=timezone.now(),
        )

        response = self.client.get(reverse("admin_payments"), {
            "from_date": selected_date.isoformat(),
            "to_date": selected_date.isoformat(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_revenue"], Decimal("10.00"))
        self.assertEqual(response.context["payment_count"], 1)


class AdminRoleAccessTests(TestCase):
    def setUp(self):
        self.admin_group, _ = Group.objects.get_or_create(name="Admin")
        self.suit = Suit.objects.create(
            suit_name="Role Test Suit",
            price_per_day=Decimal("20.00"),
            quantity=2,
            status="Available",
        )

    def create_staff(self, username):
        return User.objects.create_user(username=username, password="testpass123", is_staff=True)

    def test_admin_group_alone_cannot_grant_admin_inventory_access(self):
        user = User.objects.create_user(username="unassigned_staff", password="testpass123")
        user.groups.add(self.admin_group)
        self.client.login(username=user.username, password="testpass123")

        response = self.client.get(reverse("inventory"))

        self.assertRedirects(response, reverse("home"))

    def test_reception_group_without_profile_role_does_not_grant_admin_access(self):
        user = self.create_staff("group_reception")
        reception_group, _ = Group.objects.get_or_create(name="Reception")
        user.groups.add(reception_group)
        self.client.login(username=user.username, password="testpass123")

        response = self.client.get(reverse("inventory"))

        self.assertRedirects(response, reverse("home"))

    def test_staff_without_reception_or_cashier_role_can_access_admin_inventory(self):
        user = self.create_staff("inventory_admin")
        user.groups.add(self.admin_group)
        self.client.login(username=user.username, password="testpass123")

        response = self.client.get(reverse("inventory"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/inventory_admin.html")
        self.assertContains(response, reverse("edit_suit_new", args=[0]))

    def test_admin_pages_share_the_same_professional_sidebar(self):
        user = self.create_staff("sidebar_admin")
        self.client.login(username=user.username, password="testpass123")

        for url_name in ("inventory", "manage_users", "admin_profile"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "rental/base_admin.html")
                self.assertContains(response, "ROYAL RENTAL SUITS")
                self.assertContains(response, "Users & Customers")
                self.assertContains(response, "Dry Cleaning Ledger")
                self.assertContains(response, "Create Booking")
                self.assertNotContains(response, "Reception Dashboard")
                self.assertNotContains(response, "Reception Desk")
                self.assertContains(response, reverse("inventory_movements"))
                if url_name == "admin_profile":
                    self.assertContains(response, "Late-Fee Business Rule")

    def test_customers_page_uses_admin_sidebar_and_highlights_current_page(self):
        user = self.create_staff("customers_sidebar_admin")
        self.client.login(username=user.username, password="testpass123")

        response = self.client.get(reverse("customers"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "rental/base_admin.html")
        self.assertContains(response, "ROYAL RENTAL SUITS")
        self.assertContains(response, f'href="{reverse("customers")}" class="active"')
        self.assertContains(response, "color: #ffe17a;")

    def test_admin_sidebar_exposes_booking_creation_but_not_reception_interface(self):
        user = self.create_staff("admin_sidebar_navigation")
        self.client.login(username=user.username, password="testpass123")

        response = self.client.get(reverse("admin_booking"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Booking")
        self.assertContains(response, f'href="{reverse("admin_booking")}" class="active"')
        self.assertNotContains(response, "Reception Dashboard")
        self.assertNotContains(response, "Reception Desk")

        for path in ("/reception-dashboard/", "/reception-reception/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_reception_sidebar_hides_admin_navigation_and_stays_in_reception_area(self):
        receptionist = self.create_staff("sidebar_reception")
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])
        self.client.force_login(receptionist)

        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, reverse("home"))
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "Reception Dashboard")
        self.assertNotContains(response, "Admin Dashboard")
        self.assertNotContains(response, "Create Booking")
        self.assertNotContains(response, "Users & Customers")

    def test_home_navigation_and_book_now_are_role_aware(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Book Now")

        customer = User.objects.create_user(username="home_customer", password="customer-pass")
        self.client.force_login(customer)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Book Now")
        self.assertNotContains(response, "Admin Dashboard")
        self.assertNotContains(response, "Reception Dashboard")
        self.assertNotContains(response, f'href="{reverse("inventory")}"')

        admin = self.create_staff("home_admin")
        self.client.force_login(admin)
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "Book Now")
        self.assertContains(response, "Admin Dashboard")
        self.assertContains(response, "Create Booking")
        self.assertNotContains(response, "Reception Dashboard")
        for page_name in ("all_suits", "service_page", "about_page", "contact_page"):
            with self.subTest(role="admin", page=page_name):
                response = self.client.get(reverse(page_name))
                if page_name == "all_suits":
                    self.assertNotContains(
                        response,
                        f'href="{reverse("request_form", args=[self.suit.pk])}"',
                    )
                else:
                    self.assertNotContains(
                        response,
                        f'class="nav-link btn-nav" href="{reverse("all_suits")}">Book Now</a>',
                    )

        receptionist = self.create_staff("home_reception")
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])
        self.client.force_login(receptionist)
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "Book Now")
        self.assertNotContains(response, "Reception Dashboard")
        self.assertNotContains(response, "Admin Dashboard")
        self.assertNotContains(response, "Create Booking")
        for page_name in ("all_suits", "service_page", "about_page", "contact_page"):
            with self.subTest(role="reception", page=page_name):
                response = self.client.get(reverse(page_name))
                if page_name == "all_suits":
                    self.assertNotContains(
                        response,
                        f'href="{reverse("request_form", args=[self.suit.pk])}"',
                    )
                else:
                    self.assertNotContains(
                        response,
                        f'class="nav-link btn-nav" href="{reverse("all_suits")}">Book Now</a>',
                    )

    def test_reception_account_cannot_access_admin_dashboard(self):
        receptionist = self.create_staff("dashboard_reception_redirect")
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])
        self.client.force_login(receptionist)

        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, reverse("home"))

    def test_staff_cannot_open_or_submit_customer_booking_forms(self):
        admin = self.create_staff("booking_route_admin")
        receptionist = self.create_staff("booking_route_reception")
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])

        for user, destination in (
            (admin, "dashboard"),
            (receptionist, "home"),
        ):
            with self.subTest(role=user.username):
                self.client.force_login(user)
                form_response = self.client.get(reverse("request_form", args=[self.suit.pk]))
                post_response = self.client.post(reverse("book_suit"), {
                    "suit_id": self.suit.pk,
                    "full_name": "Staff Attempt",
                    "phone": "252611223344",
                    "address": "Hargeisa",
                })

                self.assertRedirects(form_response, reverse(destination))
                self.assertRedirects(post_response, reverse(destination))
                self.assertFalse(SuitRequest.objects.filter(name="Staff Attempt").exists())

                collection_response = self.client.get(reverse("all_suits"))
                self.assertEqual(collection_response.status_code, 200)
                self.assertNotContains(collection_response, "Request Rental")

    def test_return_forms_receive_fee_policy_and_idempotency_key(self):
        admin = self.create_staff("return_form_admin")
        self.client.force_login(admin)
        admin_response = self.client.get(reverse("requests"))

        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, 'data-late-fee-rate="0.50"')
        self.assertContains(admin_response, 'data-partial-hour-policy="round_up"')
        self.assertRegex(
            admin_response.content.decode(),
            r'name="payment_idempotency_key" value="[0-9a-f-]{36}"',
        )

        receptionist = self.create_staff("return_form_reception")
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])
        self.client.force_login(receptionist)
        self.assertEqual(self.client.get("/reception-requests/").status_code, 404)

    def test_reception_role_cannot_access_admin_inventory_even_with_admin_group(self):
        user = self.create_staff("reception_user")
        user.groups.add(self.admin_group)
        profile = user.profile
        profile.is_reception = True
        profile.save(update_fields=["is_reception"])
        self.client.login(username=user.username, password="testpass123")

        admin_response = self.client.get(reverse("inventory"))
        self.assertRedirects(admin_response, reverse("home"))
        self.assertEqual(self.client.get("/reception-inventory/").status_code, 404)


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

    def test_inventory_movement_history_shows_and_filters_records(self):
        suit = Suit.objects.get(suit_name="Admin Suit")
        InventoryMovement.objects.create(
            suit=suit,
            suit_name=suit.suit_name,
            movement_type="issued",
            quantity_delta=-1,
            quantity_after=1,
            performed_by=self.user,
            notes="Issued for wedding rental",
        )
        InventoryMovement.objects.create(
            suit=suit,
            suit_name=suit.suit_name,
            movement_type="adjustment",
            quantity_delta=2,
            quantity_after=3,
            performed_by=self.user,
            notes="Stock count correction",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory_movements"), {
            "q": "wedding",
            "type": "issued",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/inventory_movements.html")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, "Issued to Customer")
        self.assertContains(response, "Stock after")
        self.assertNotContains(response, "Stock count correction")

    def test_inventory_movement_history_is_admin_only(self):
        receptionist = User.objects.create_user(username="history_reception", password="testpass123")
        receptionist.is_staff = True
        receptionist.save(update_fields=["is_staff"])
        receptionist.profile.is_reception = True
        receptionist.profile.save(update_fields=["is_reception"])
        self.client.force_login(receptionist)

        response = self.client.get(reverse("inventory_movements"))

        self.assertRedirects(response, reverse("home"))

    def test_admin_can_configure_late_fee_rate_and_partial_hour_policy(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("admin_profile"), {
            "late_fee_per_hour": "1.25",
            "partial_hour_policy": "prorate",
        })

        policy = RentalPolicy.get_solo()
        self.assertRedirects(response, reverse("admin_profile"))
        self.assertEqual(policy.late_fee_per_hour, Decimal("1.25"))
        self.assertEqual(policy.partial_hour_policy, "prorate")

    def test_prorated_late_fee_uses_configured_hourly_rate(self):
        policy = RentalPolicy.get_solo()
        policy.late_fee_per_hour = Decimal("1.25")
        policy.partial_hour_policy = "prorate"
        policy.save()
        suit = Suit.objects.get(suit_name="Admin Suit")
        due_time = timezone.now() - timezone.timedelta(minutes=30)
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Prorated Fee Customer",
            phone="252611223355",
            status="Active",
            quantity_requested=1,
            days_requested=1,
            due_date=due_time,
            end_time=due_time,
        )

        self.assertEqual(rental.late_fee, Decimal("0.63"))

    def test_rental_inventory_actions_create_auditable_movement_records(self):
        self.client.force_login(self.user)
        suit = Suit.objects.create(
            suit_name="Ledger Accessory",
            item_type="ties",
            price_per_day=Decimal("10.00"),
            quantity=3,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Ledger Customer",
            phone="252611223344",
            quantity_requested=1,
            days_requested=1,
            total_amount=Decimal("10.00"),
            status="Pending",
        )

        rental.reserve_inventory(timezone.now() + timezone.timedelta(hours=1), self.user)
        rental.start_rental(performed_by=self.user)
        rental.return_suit(performed_by=self.user)

        movements = list(
            InventoryMovement.objects.filter(rental_request=rental)
            .order_by("pk")
            .values_list("movement_type", "quantity_delta", "quantity_after", "performed_by_id")
        )
        self.assertEqual(
            movements,
            [
                ("reserved", 0, 3, self.user.pk),
                ("issued", -1, 2, self.user.pk),
                ("returned", 1, 3, self.user.pk),
            ],
        )

    def test_damaged_accessory_return_is_held_out_of_available_stock(self):
        suit = Suit.objects.create(
            suit_name="Damaged Return Shoes",
            item_type="shoes",
            price_per_day=Decimal("10.00"),
            quantity=0,
            status="Rented",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Damaged Customer",
            phone="252611223366",
            quantity_requested=1,
            days_requested=1,
            total_amount=Decimal("10.00"),
            status="Active",
        )

        rental.return_suit(performed_by=self.user, return_condition="damaged")

        suit.refresh_from_db()
        movement = InventoryMovement.objects.get(rental_request=rental, movement_type="returned_for_cleaning")
        self.assertEqual(suit.status, "Dry Cleaning")
        self.assertEqual(suit.quantity, 0)
        self.assertEqual(movement.quantity_delta, 0)
        self.assertEqual(movement.performed_by, self.user)

    def test_stock_adjustment_cannot_reduce_unexpired_reservations(self):
        self.client.force_login(self.user)
        suit = Suit.objects.create(
            suit_name="Reserved Stock Suit",
            price_per_day=Decimal("30.00"),
            quantity=3,
            status="Available",
        )
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Reserved Customer",
            phone="252611223344",
            quantity_requested=2,
            days_requested=1,
            total_amount=Decimal("60.00"),
            status="Pending",
        )
        rental.reserve_inventory(timezone.now() + timezone.timedelta(hours=1), self.user)

        response = self.client.post(reverse("edit_suit_new", args=[suit.pk]), {"quantity": "1"})

        suit.refresh_from_db()
        self.assertRedirects(response, reverse("edit_suit_new", args=[suit.pk]))
        self.assertEqual(suit.quantity, 3)
        self.assertFalse(
            InventoryMovement.objects.filter(suit=suit, movement_type="adjustment").exists()
        )

    def test_adding_inventory_records_opening_stock_and_responsible_user(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("add_suit_new"), {
            "suit_name": "Opening Stock Suit",
            "collection": "Standard",
            "category": "suits",
            "color": "Navy",
            "size": "M",
            "quantity": "4",
            "price_per_day": "20.00",
        })

        suit = Suit.objects.get(suit_name="Opening Stock Suit")
        movement = InventoryMovement.objects.get(suit=suit)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(movement.movement_type, "opening_balance")
        self.assertEqual(movement.quantity_delta, 4)
        self.assertEqual(movement.quantity_after, 4)
        self.assertEqual(movement.performed_by, self.user)

    def test_admin_approval_redirects_back_to_requests_list(self):
        self.reception_user = User.objects.create_user(username="reception", password="testpass123")
        self.reception_user.is_staff = True
        self.reception_user.save(update_fields=["is_staff"])
        self.reception_profile = self.reception_user.profile
        self.reception_profile.is_reception = False
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

        self.assertRedirects(response, reverse("requests"))

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
                "first_name": "Jane",
                "second_name": "Doe",
                "email": "jane@example.com",
                "phone": "252611223344",
                "present_address": "Hargeisa",
                "parents_address": "Mogadishu",
                "national_id": "ID-12345",
                "days_requested": "2",
                "quantity_requested": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        booking = SuitRequest.objects.get(user=customer, suit=suit)
        self.assertEqual(booking.name, "Jane Doe")
        self.assertEqual(booking.customer_email, "jane@example.com")
        customer.refresh_from_db()
        self.assertEqual(customer.first_name, "Jane")
        self.assertEqual(customer.last_name, "Doe")
        self.assertEqual(customer.profile.customer_phone, "252611223344")
        self.assertEqual(customer.profile.present_address, "Hargeisa")
        self.assertEqual(customer.profile.parents_address, "Mogadishu")
        self.assertEqual(customer.profile.national_id, "ID-12345")

        history_response = self.client.get(reverse("customer_history"))
        self.assertContains(history_response, "Jane Doe")
        self.assertContains(history_response, "Booking Suit")
        self.assertContains(history_response, "Your saved profile")
        self.assertContains(history_response, "Hargeisa")

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
        customer.refresh_from_db()
        self.assertEqual(customer.profile.customer_phone, "252611223344")
        self.assertEqual(customer.profile.present_address, "Mogadishu")

    def test_request_form_prefills_saved_customer_profile(self):
        customer = User.objects.create_user(
            username="saved-customer",
            password="testpass123",
            first_name="Ayaan",
            last_name="Ali",
            email="ayaan@example.com",
        )
        customer.profile.customer_phone = "252611223344"
        customer.profile.national_id = "ID-12345"
        customer.profile.present_address = "Hargeisa"
        customer.profile.parents_address = "Mogadishu"
        customer.profile.save()
        suit = Suit.objects.create(
            suit_name="Prefilled Profile Suit",
            price_per_day=Decimal("25.00"),
            quantity=2,
            status="Available",
        )
        self.client.force_login(customer)

        response = self.client.get(reverse("request_suit", args=[suit.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Ayaan Ali"')
        self.assertContains(response, 'value="ayaan@example.com"')
        self.assertContains(response, 'value="252611223344"')
        self.assertContains(response, 'value="Hargeisa"')
        self.assertContains(response, 'value="Mogadishu"')
        self.assertContains(response, 'value="ID-12345"')

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
        customer = User.objects.create_user(username="catalog_customer", password="testpass123")
        self.client.force_login(customer)
        suit = Suit.objects.create(suit_name="Direct Booking Suit", price_per_day=Decimal("18.00"), quantity=2, status="Available")

        response = self.client.get(reverse("all_suits"))

        self.assertContains(response, f'/request-form/{suit.id}/')

    def test_admin_approval_with_accessories_updates_total_and_status(self):
        reception_user = User.objects.create_user(username="reception", password="testpass123")
        reception_user.is_staff = True
        reception_user.save(update_fields=["is_staff"])
        reception_user.profile.is_reception = False
        reception_user.profile.save(update_fields=["is_reception"])
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

        form_response = self.client.get(reverse("approve_request", args=[req.id]))
        self.assertContains(form_response, 'value="watch" id="acc_watch"')
        self.assertContains(form_response, 'data-price="5.00"')

        response = self.client.post(
            reverse("approve_request", args=[req.id]),
            {
                "action_mode": "rent_now",
                "start_date": "2026-07-05",
                "amount_paid": "59",
                "discount_type": "percent",
                "discount_value": "10",
                "payment_method": "cash",
                "payment_reference": "",
                "accessories": ["shoes", "tie", "belt", "watch"],
                "addon_price_shoes": "5.00",
                "guarantee_tie": "2.00",
                "guarantee_belt": "1.00",
                "guarantee_watch": "2.00",
                "shoes_size": "42",
                "shoes_color": "Black",
                "tie_size": "Slim",
                "tie_color": "Red",
                "belt_size": "38",
                "belt_color": "Black",
                "watch_size": "Classic",
                "watch_color": "Black",
            },
        )

        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "Active")
        self.assertEqual(req.total_amount, Decimal("54.00"))
        self.assertEqual(req.discount_amount, Decimal("6.00"))
        self.assertEqual(req.guarantee_total, Decimal("5.00"))
        self.assertEqual(req.amount_paid, Decimal("54.00"))
        self.assertEqual(req.guarantee_collected, Decimal("5.00"))
        self.assertIn("Accessories:", req.notes)
        self.assertIn("shoes", req.notes.lower())
        self.assertIn("watch", req.notes.lower())

    def test_admin_booking_prices_only_shoes_and_collects_accessory_guarantees(self):
        suit = Suit.objects.get(suit_name="Admin Suit")
        self.client.force_login(self.user)
        form_response = self.client.get(reverse("admin_booking"))
        self.assertContains(form_response, "Watch (Saacad)")
        self.assertContains(form_response, 'name="suit_price"')
        self.assertContains(form_response, 'name="guarantee_tie"')
        self.assertContains(form_response, 'name="addon_price_shoes"')

        response = self.client.post(
            reverse("create_rental"),
            {
                "suit_id": str(suit.pk),
                "suit_price": "21.25",
                "quantity": "2",
                "days": "2",
                "start_date": timezone.localdate().isoformat(),
                "end_date": (timezone.localdate() + timezone.timedelta(days=2)).isoformat(),
                "first_name": "Accessory",
                "father_name": "Customer",
                "phone": "252611223344",
                "amount_paid": "117.50",
                "discount_type": "percent",
                "discount_value": "10",
                "addons[]": ["tie", "belt", "watch", "shoes"],
                "addon_price_tie": "0.01",
                "addon_price_belt": "0.01",
                "addon_price_watch": "0.01",
                "addon_price_shoes": "7.50",
                "guarantee_tie": "2.00",
                "guarantee_belt": "3.00",
                "guarantee_watch": "4.00",
                "guarantee_shoes": "5.00",
                "watch_size": "Classic",
                "watch_color": "Black",
            },
        )

        self.assertEqual(response.status_code, 302)
        rental = SuitRequest.objects.get(name="Accessory Customer")
        self.assertEqual(rental.suit_price_per_day, Decimal("21.25"))
        self.assertEqual(rental.total_amount, Decimal("103.50"))
        self.assertEqual(rental.discount_amount, Decimal("11.50"))
        self.assertEqual(rental.guarantee_total, Decimal("14.00"))
        self.assertEqual(rental.amount_paid, Decimal("103.50"))
        self.assertEqual(rental.guarantee_collected, Decimal("14.00"))
        payment = rental.payments.get()
        self.assertEqual(payment.amount_paid, Decimal("103.50"))
        self.assertEqual(payment.guarantee_amount, Decimal("14.00"))
        self.assertEqual(
            set(rental.accessory_items),
            {
                "tie (Standard / Standard)",
                "belt (Standard / Standard)",
                "watch (Classic / Black)",
                "shoes (Standard / Standard)",
            },
        )

    def test_accessory_only_booking_uses_custom_price_fixed_discount_and_365_days(self):
        self.client.force_login(self.user)
        start_date = timezone.localdate()
        response = self.client.post(
            reverse("create_rental"),
            {
                "suit_id": "",
                "quantity": "1",
                "days": "365",
                "start_date": start_date.isoformat(),
                "end_date": (start_date + timezone.timedelta(days=365)).isoformat(),
                "first_name": "Accessory",
                "father_name": "Only",
                "phone": "252611223344",
                "amount_paid": "1181.25",
                "discount_type": "amount",
                "discount_value": "10.00",
                "addons[]": ["watch"],
                "addon_price_watch": "3.25",
                "guarantee_watch": "5.00",
                "watch_size": "Classic",
                "watch_color": "Black",
            },
        )

        self.assertEqual(response.status_code, 302)
        rental = SuitRequest.objects.get(name="Accessory Only")
        self.assertIsNone(rental.suit)
        self.assertEqual(rental.days_requested, 365)
        self.assertEqual(rental.total_amount, Decimal("1176.25"))
        self.assertEqual(rental.discount_amount, Decimal("10.00"))
        self.assertEqual(rental.guarantee_total, Decimal("5.00"))
        self.assertEqual(rental.amount_paid, Decimal("1176.25"))
        self.assertEqual(rental.guarantee_collected, Decimal("5.00"))
        self.assertEqual(
            rental.accessory_details["watch"]["price_per_day"],
            "3.25",
        )
        payment = rental.payments.get()
        self.assertEqual(payment.amount_paid, Decimal("1176.25"))
        self.assertEqual(payment.guarantee_amount, Decimal("5.00"))
        self.assertEqual(Suit.objects.get(suit_name="Admin Suit").quantity, 2)

        return_response = self.client.post(
            reverse("process_return"),
            {
                "rental_id": rental.pk,
                "return_condition": "good",
                "return_amount_paid": "0.00",
                "payment_method": "cash",
            },
        )
        self.assertEqual(return_response.status_code, 302)
        rental.refresh_from_db()
        self.assertEqual(rental.guarantee_refunded, Decimal("5.00"))
        self.assertEqual(rental.status, "Returned")

    def test_return_applies_guarantee_to_damage_and_records_refund(self):
        self.client.force_login(self.user)
        suit = Suit.objects.get(suit_name="Admin Suit")
        rental = SuitRequest.objects.create(
            suit=suit,
            name="Guarantee Customer",
            phone="252611223344",
            days_requested=1,
            quantity_requested=1,
            total_amount=Decimal("30.00"),
            guarantee_total=Decimal("10.00"),
            status="Active",
            rental_start_time=timezone.now(),
            due_date=timezone.now() + timezone.timedelta(days=1),
            end_time=timezone.now() + timezone.timedelta(days=1),
        )
        Payment.objects.create(
            rental=rental,
            cashier=self.user,
            amount_paid=Decimal("30.00"),
            guarantee_amount=Decimal("10.00"),
            payment_method="cash",
            status="paid",
        )

        response = self.client.post(
            reverse("process_return"),
            {
                "rental_id": rental.pk,
                "return_condition": "damaged",
                "requires_laundry": "on",
                "laundry_fee": "4.00",
                "return_amount_paid": "0.00",
                "payment_method": "cash",
            },
        )

        self.assertEqual(response.status_code, 302)
        rental.refresh_from_db()
        self.assertEqual(rental.status, "Returned")
        self.assertEqual(rental.guarantee_refunded, Decimal("6.00"))
        self.assertIsNotNone(rental.guarantee_settled_at)
        refund = rental.payments.get(status="refunded")
        self.assertEqual(refund.refund_amount, Decimal("6.00"))
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
