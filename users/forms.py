from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Address, CustomUser, Profile


class SignUpForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150, required=False, label='First name')
    last_name = forms.CharField(max_length=150, required=False, label='Last name')
    city = forms.CharField(max_length=100, required=False, label='City')
    country = forms.CharField(max_length=100, required=False, label='Country')
    phone = forms.CharField(max_length=50, required=False, label='Phone number')
    street = forms.CharField(max_length=255, required=False, label='Street')
    password1 = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm Password')

    class Meta:
        model = CustomUser
        fields = ('email', 'first_name', 'last_name')

    def clean_password2(self):
        p1 = self.cleaned_data.get('password1')
        p2 = self.cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            with transaction.atomic():
                user.save()
                Profile.objects.create(user=user, phone=self.cleaned_data.get('phone', ''))
                Address.objects.create(
                    user=user,
                    label='Registration',
                    street=self.cleaned_data.get('street', ''),
                    city=self.cleaned_data.get('city', ''),
                    country=self.cleaned_data.get('country', ''),
                    is_primary=True,
                )
        return user


class ProfileForm(forms.ModelForm):
    email = forms.EmailField(required=True, label='Email address')

    class Meta:
        model = Profile
        fields = ('bio', 'profile_picture', 'phone')


class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = ('label', 'street', 'city', 'postal_code', 'country', 'is_primary')