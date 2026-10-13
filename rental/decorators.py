from functools import wraps
from django.shortcuts import redirect
from django.conf import settings

# 🔹 Decorator: Admin Only
def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(settings.LOGIN_URL or 'admin_login')
        profile = getattr(request.user, 'profile', None)
        if not request.user.is_staff or (
            profile and (
                getattr(profile, 'is_reception', False)
                or getattr(profile, 'is_cashier', False)
            )
        ) or request.user.groups.filter(name__in=('Cashier', 'Reception')).exists():
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapper

def get_pending_count():
    try:
        from .models import SuitRequest
        return SuitRequest.objects.filter(status='Pending').count()
    except Exception:
        return 0
