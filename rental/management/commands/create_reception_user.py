from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from rental.models import Profile


class Command(BaseCommand):
    help = 'Create or update a reception staff user (default username/password: reception/reception)'

    def add_arguments(self, parser):
        parser.add_argument('--username', default='reception', help='Username for the reception user')
        parser.add_argument('--password', default='reception', help='Password for the reception user')
        parser.add_argument('--email', default='', help='Optional email')

    def handle(self, *args, **options):
        username = options['username']
        password = options['password']
        email = options.get('email') or ''

        if User.objects.filter(username__iexact=username).exists():
            user = User.objects.get(username__iexact=username)
            user.set_password(password)
            user.email = email
            user.is_active = True
            user.is_staff = True
            user.save()
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.is_reception = True
            profile.is_cashier = False
            profile.save()
            self.stdout.write(self.style.SUCCESS(f"Updated existing user '{username}' as reception staff."))
            return

        user = User.objects.create_user(username=username, password=password, email=email)
        user.is_active = True
        user.is_staff = True
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.is_reception = True
        profile.is_cashier = False
        profile.save()

        self.stdout.write(self.style.SUCCESS(f"Created reception user '{username}' (password: '{password}')."))
