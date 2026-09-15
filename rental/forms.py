import os
import re

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal
from .models import Suit, Profile


# Canonical allowed image types (ordered for user-facing messages)
ALLOWED_IMAGE_ORDERED = ['bmp', 'gif', 'jpeg', 'jpg', 'jfif', 'png', 'svg', 'webp']
ALLOWED_IMAGE_EXTENSIONS = set(ALLOWED_IMAGE_ORDERED)
MAX_IMAGE_SIZE = 2 * 1024 * 1024  # 2MB


def validate_image_upload(uploaded_file, field_name="image"):
    if not uploaded_file:
        return None

    filename = getattr(uploaded_file, 'name', '')
    extension = os.path.splitext(filename)[1].lstrip('.').lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        # For the common "suit image" field, show types in a fixed preferred order
        if field_name.replace('_', ' ') == 'suit image':
            allowed_display = ', '.join(ext.upper() for ext in ALLOWED_IMAGE_ORDERED)
        else:
            allowed_display = ', '.join(ext.upper() for ext in sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise forms.ValidationError(
            f"Only the following image types are allowed for {field_name.replace('_', ' ')}: {allowed_display}."
        )

    if uploaded_file.size > MAX_IMAGE_SIZE:
        raise forms.ValidationError(
            f"{field_name.replace('_', ' ').capitalize()} cannot exceed 2 MB."
        )

    # Allow common customer-uploaded photos even if the browser uses .jfif or similar image formats.
    if field_name.replace('_', ' ') == 'customer image':
        return uploaded_file

    # Additional safety checks: content type and file header
    content_type = getattr(uploaded_file, 'content_type', '') or ''
    if not content_type.startswith('image/') and extension != 'svg':
        raise forms.ValidationError(f"Uploaded {field_name.replace('_',' ')} must be an image file.")

    # Verify image header where possible (note: imghdr doesn't detect SVG)
    try:
        import imghdr
        file_obj = getattr(uploaded_file, 'file', None)
        if file_obj:
            # read a small header and reset pointer
            current_pos = None
            try:
                current_pos = file_obj.tell()
            except Exception:
                current_pos = None
            header = file_obj.read(512)
            # reset pointer if possible
            try:
                if current_pos is not None:
                    file_obj.seek(current_pos)
                else:
                    file_obj.seek(0)
            except Exception:
                pass

            detected = imghdr.what(None, header)
            if not detected and extension != 'svg':
                raise forms.ValidationError(f"Uploaded {field_name.replace('_',' ')} does not appear to be a valid image.")
    except forms.ValidationError:
        raise
    except Exception:
        # If header check fails for unexpected reasons, allow based on extension and content-type
        pass

    return uploaded_file


# ============ VALIDATION FUNCTIONS ============

def validate_username(username):
    """Validate username: 3-20 chars, letters/numbers/underscore only"""
    if not username or len(username) < 3:
        raise ValidationError("Username must be at least 3 characters long.")
    
    if len(username) > 20:
        raise ValidationError("Username must not exceed 20 characters.")
    
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        raise ValidationError("Username can only contain letters, numbers, and underscores.")
    
    if User.objects.filter(username=username).exists():
        raise ValidationError("This username is already taken.")


def validate_email_format(email):
    """Validate email format"""
    if not email:
        raise ValidationError("Email is required.")
    
    if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
        raise ValidationError("Please enter a valid email address.")
    
    if User.objects.filter(email=email).exists():
        raise ValidationError("An account with this email already exists.")


def validate_password_strength(password):
    """
    Validate password strength requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character (@$!%*?&.#)
    """
    errors = []
    
    if not password or len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    
    if not re.search(r'[A-Z]', password):
        errors.append("Password must contain at least one uppercase letter.")
    
    if not re.search(r'[a-z]', password):
        errors.append("Password must contain at least one lowercase letter.")
    
    if not re.search(r'\d', password):
        errors.append("Password must contain at least one number.")
    
    if not re.search(r'[@$!%*?&.#]', password):
        errors.append("Password must contain at least one special character (@$!%*?&.#).")
    
    if errors:
        raise ValidationError(errors)


# ============ FORMS ============

class UserRegistrationForm(forms.Form):
    """User registration form with comprehensive validation"""
    username = forms.CharField(
        max_length=20,
        min_length=3,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Choose a username',
            'autocomplete': 'username'
        })
    )
    
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'your.email@example.com',
            'autocomplete': 'email'
        })
    )
    
    password = forms.CharField(
        max_length=128,
        required=True,
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Create a strong password',
            'autocomplete': 'new-password'
        })
    )
    
    password2 = forms.CharField(
        max_length=128,
        required=True,
        label='Confirm Password',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm your password',
            'autocomplete': 'new-password'
        })
    )

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        validate_username(username)
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        validate_email_format(email)
        return email

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        validate_password_strength(password)
        return password

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password2 = cleaned_data.get('password2')

        if password and password2:
            if password != password2:
                raise ValidationError("Passwords do not match. Please try again.")

        return cleaned_data


class SuitForm(forms.ModelForm):
    class Meta:
        model = Suit
        fields = ['suit_name', 'collection', 'size', 'color', 'price_per_day', 'image', 
                  'quantity', 'description', 'status', 'condition_grade', 'damage_notes']
        widgets = {
            'suit_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Classic Navy'
            }),
            'collection': forms.Select(attrs={'class': 'form-control'}),
            'size': forms.Select(attrs={'class': 'form-control'}),
            'color': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Navy Blue'
            }),
            'price_per_day': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.00',
                'step': '0.01',
                'min': '0'
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
                'value': '1'
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Brief description of the suit...'
            }),
            'image': forms.FileInput(attrs={
                'class': 'form-control',
                'accept': 'image/*'
            }),
            'status': forms.Select(attrs={'class': 'form-control'}),
            'condition_grade': forms.Select(attrs={'class': 'form-control'}),
            'damage_notes': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Note any damage, repairs, or maintenance issues...'
            }),
        }

    def clean_suit_name(self):
        suit_name = self.cleaned_data.get('suit_name', '').strip()
        if not suit_name:
            raise forms.ValidationError("Suit name is required.")
        if not re.match(r'^[A-Za-z0-9 .,&()\'"-]+$', suit_name):
            raise forms.ValidationError("Suit name contains invalid characters.")
        return suit_name

    def clean_image(self):
        return validate_image_upload(self.cleaned_data.get('image'), 'suit image')

    def clean(self):
        cleaned_data = super().clean()
        collection = cleaned_data.get('collection')
        price = cleaned_data.get('price_per_day')

        # If there's no price provided (should normally be required for suits), skip
        if price in (None, ''):
            return cleaned_data

        # Ensure using Decimal comparisons
        try:
            price_val = Decimal(price)
        except Exception:
            raise ValidationError({'price_per_day': 'Enter a valid price.'})

        if collection == 'Luxury':
            if not (Decimal('25') <= price_val <= Decimal('100')):
                raise ValidationError({'price_per_day': 'Luxury suits price must be between $25 and $100.'})
        elif collection == 'Standard':
            if not (Decimal('15') <= price_val <= Decimal('25')):
                raise ValidationError({'price_per_day': 'Standard suits price must be between $15 and $25.'})
        elif collection == 'Budget':
            if not (Decimal('14') <= price_val <= Decimal('18')):
                raise ValidationError({'price_per_day': 'Budget suits price must be between $14 and $18.'})

        return cleaned_data


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['profile_photo', 'id_document']
        widgets = {
            'profile_photo': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'id_document': forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }

    def clean_profile_photo(self):
        return validate_image_upload(self.cleaned_data.get('profile_photo'), 'profile photo')

    def clean_id_document(self):
        return validate_image_upload(self.cleaned_data.get('id_document'), 'ID document')
