from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal

from rental.models import Staff


class Command(BaseCommand):
    help = 'Seed example staff records (creates 3 entries if none exist)'

    def handle(self, *args, **options):
        if Staff.objects.exists():
            self.stdout.write(self.style.WARNING('Staff records already exist — no changes made.'))
            return

        staff_data = [
            {'name': 'Ayaan Mohamed', 'phone': '+252612345678', 'position': 'Manager', 'monthly_salary': Decimal('500.00'), 'status': 'active'},
            {'name': 'Hassan Ali', 'phone': '+252699112233', 'position': 'Receptionist', 'monthly_salary': Decimal('250.00'), 'status': 'active'},
            {'name': 'Fatima Aden', 'phone': '+252612112233', 'position': 'Cashier', 'monthly_salary': Decimal('300.00'), 'status': 'active'},
        ]

        for s in staff_data:
            Staff.objects.create(**s)

        self.stdout.write(self.style.SUCCESS(f'Created {len(staff_data)} staff records'))
