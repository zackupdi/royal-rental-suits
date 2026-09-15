from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from rental.models import SuitRequest, Notification
from django.contrib.auth.models import User
from rental.sms import send_sms


class Command(BaseCommand):
    help = 'Check for expired rentals and send notifications'

    def handle(self, *args, **options):
        # Find all active rentals that have expired
        expired_rentals = SuitRequest.objects.filter(
            status='Active',
            return_date__lt=timezone.now()
        )

        notified_count = 0
        for rental in expired_rentals:
            # Prepare notification message
            message = f"Way gaadhay wakhtiga kirada ee suit-ka '{rental.suit.suit_name}'. Fadlan soo celi ama la xiriiro si dib loogu heli karo. Number-ka: {rental.phone}"

            # Create internal Notification record
            Notification.objects.create(
                user=rental.user,
                message=message
            )


            # Mark rental as expired so admin/cashier can see it
            if rental.status != 'Expired':
                rental.status = 'Expired'
                # calculate late fee up to now and apply to total_amount
                try:
                    fee = rental._calculate_late_fee(timezone.now())
                except Exception:
                    fee = 0

                if fee and fee > 0:
                    rental.total_amount = (rental.total_amount or 0) + fee

                # save status and updated total
                rental.save(update_fields=['status', 'total_amount'])

                # notify staff users about expired rental with applied fee
                staff_users = User.objects.filter(is_staff=True)
                for staff in staff_users:
                    Notification.objects.create(
                        user=staff,
                        message=f"Rental #{rental.id} for {rental.suit.suit_name} has expired. Late fee applied: ${fee}."
                    )

            # Send SMS if not already notified and SMS configured
            try:
                if not getattr(rental, 'is_notified', False):
                    sent, info = send_sms(rental.phone, message)
                    if sent:
                        rental.is_notified = True
                        rental.save(update_fields=['is_notified'])
                    else:
                        self.stdout.write(self.style.WARNING(f"SMS not sent for {rental}: {info}"))
            except Exception as e:
                # Don't stop the whole loop if SMS fails; log to stdout
                self.stdout.write(self.style.WARNING(f"Failed to send SMS for {rental}: {e}"))

            notified_count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f'Sent notification for expired rental "{rental.suit.suit_name}" '
                    f'to customer "{rental.name}"'
                )
            )

        if notified_count == 0:
            self.stdout.write('No expired rentals found.')
        else:
            self.stdout.write(
                self.style.SUCCESS(f'Sent notifications for {notified_count} expired rentals.')
            )