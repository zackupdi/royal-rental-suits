from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = 'Create or list admin users'

    def add_arguments(self, parser):
        parser.add_argument('--create', action='store_true', help='Create a new admin user')
        parser.add_argument('--username', type=str, help='Admin username')
        parser.add_argument('--password', type=str, help='Admin password')
        parser.add_argument('--email', type=str, default='admin@suitrental.com', help='Admin email')

    def handle(self, *args, **options):
        if options['create']:
            username = options['username'] or input('Enter admin username: ')
            password = options['password'] or input('Enter admin password: ')
            email = options['email']
            
            if User.objects.filter(username=username).exists():
                self.stdout.write(self.style.ERROR(f'User "{username}" already exists'))
                return
            
            user = User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f'Admin user "{username}" created successfully!'))
        else:
            # List all admin users
            admins = User.objects.filter(is_staff=True)
            if admins.exists():
                self.stdout.write(self.style.SUCCESS('=== Admin Users ==='))
                for admin in admins:
                    self.stdout.write(f'  • {admin.username} (Email: {admin.email})')
            else:
                self.stdout.write(self.style.WARNING('No admin users found. Create one with --create flag'))
