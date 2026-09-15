from django.contrib import admin
from .models import Suit, SuitRequest, Notification, Payment, Expense, ContactMessage, Staff, Payroll

@admin.register(Suit)
class SuitAdmin(admin.ModelAdmin):
    list_display = ('suit_name', 'collection', 'size', 'color', 'price_per_day', 'status')
    list_filter = ('collection', 'status', 'size', 'color')
    search_fields = ('suit_name', 'collection', 'size', 'color')
    list_editable = ('status', 'price_per_day')
    ordering = ('collection', 'suit_name')

@admin.register(SuitRequest)
class SuitRequestAdmin(admin.ModelAdmin):
    list_display = ('name', 'suit', 'phone', 'days_requested', 'request_date', 'is_notified', 'status')
    list_filter = ('request_date', 'suit__collection', 'is_notified', 'status')
    search_fields = ('name', 'phone', 'suit__suit_name')
    list_editable = ('is_notified',)
    ordering = ('-request_date',)

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('receipt_number', 'rental', 'cashier', 'payment_method', 'amount_paid', 'status', 'payment_date')
    list_filter = ('status', 'payment_method', 'payment_date')
    search_fields = ('receipt_number', 'rental__name', 'rental__suit__suit_name', 'cashier__username')
    ordering = ('-payment_date',)

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('description', 'amount', 'expense_date', 'created_by')
    list_filter = ('expense_date',)
    search_fields = ('description', 'created_by__username')
    ordering = ('-expense_date',)

@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'created_at')
    list_filter = ('created_at', 'subject')
    search_fields = ('name', 'email', 'subject', 'message')
    ordering = ('-created_at',)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'message', 'created_at', 'is_read')
    list_filter = ('created_at', 'is_read')
    search_fields = ('user__username', 'message')
    list_editable = ('is_read',)
    ordering = ('-created_at',)


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ('name', 'position', 'phone', 'monthly_salary', 'status')
    search_fields = ('name', 'position', 'phone')
    list_filter = ('status', 'position')


@admin.register(Payroll)
class PayrollAdmin(admin.ModelAdmin):
    list_display = ('staff', 'payment_date', 'amount', 'payment_type', 'month', 'year', 'created_by')
    list_filter = ('payment_type', 'payment_date', 'year')
    search_fields = ('staff__name', 'notes')
