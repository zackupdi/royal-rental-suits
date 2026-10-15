import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a superuser from temporary environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("BOOTSTRAP_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("BOOTSTRAP_SUPERUSER_PASSWORD", "")
        email = os.environ.get("BOOTSTRAP_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            raise CommandError(
                "Set BOOTSTRAP_SUPERUSER_USERNAME and "
                "BOOTSTRAP_SUPERUSER_PASSWORD to create the initial admin."
            )

        user_model = get_user_model()
        existing_user = user_model.objects.filter(username__iexact=username).first()
        if existing_user:
            if existing_user.is_superuser:
                self.stdout.write("Superuser already exists; no changes made.")
                return
            raise CommandError(
                "That username already belongs to a non-superuser. "
                "Choose a different BOOTSTRAP_SUPERUSER_USERNAME."
            )

        user_model.objects.create_superuser(
            username=username,
            email=email,
            password=password,
        )
        self.stdout.write(self.style.SUCCESS("Initial superuser created."))
